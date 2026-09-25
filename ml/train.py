"""Reproducible benchmark, causal feature extraction, calibration and submission."""
import argparse, json, time
from pathlib import Path
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from ml.features import FEATURES, build_table, load_traffic, load_schedule, epoch
from ml.model import Predictor

def mae(y,p): return float(np.mean(np.abs(np.asarray(y)-np.asarray(p))))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--data',default='dataset');ap.add_argument('--out',default='artifacts');args=ap.parse_args()
    root=Path(args.data);out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    labels=pd.read_csv(root/'labels/labels_train.csv')
    test=pd.read_csv(root/'labels/labels_test.csv')
    val=pd.read_csv(root/'validate/points.csv')
    # Synthetic clones share structure with real vehicles; exclude them entirely.
    real=labels[labels.tr_id<9000000].copy()
    real['target_ts']=real.target_time_begin.map(epoch)
    real['point_ts']=real['T'].map(epoch)
    real=real.sort_values('point_ts').reset_index(drop=True)
    print('Building causal features',flush=True)
    train_x=build_table(real,load_traffic(root/'train/traffic.csv'),load_schedule(root/'train/schedule.csv'))
    test_traffic=load_traffic(root/'test/traffic.csv')
    test_x=build_table(test,test_traffic,load_schedule(root/'test/schedule.csv'))
    val_x=build_table(val,load_traffic(root/'validate/traffic.csv'),load_schedule(root/'validate/schedule_plan.csv'))
    # Purged chronological tuning and calibration; labels become available at
    # actual arrival, not merely at their planned target time.
    available=real.target_ts+np.maximum(real.target_delay_s,0)
    cut1=float(real.point_ts.quantile(.65));cut2=float(real.point_ts.quantile(.83))
    fit=available<cut1
    tune=(real.point_ts>=cut1)&(available<cut2)
    calibration=real.point_ts>=cut2
    y=real.target_delay_s.to_numpy();x=train_x.to_numpy(dtype=float)
    assert min(fit.sum(),tune.sum(),calibration.sum())>=20
    a,b=min(((a,b) for a in np.linspace(0,1,41) for b in np.arange(-40,61,5)),key=lambda ab:mae(y[fit],x[fit,0]*ab[0]+ab[1]))
    cb=CatBoostRegressor(iterations=500,depth=4,learning_rate=.035,loss_function='MAE',random_seed=42,verbose=False,thread_count=4,allow_writing_files=False,l2_leaf_reg=12)
    cb.fit(x[fit],y[fit],eval_set=(x[tune],y[tune]),early_stopping_rounds=60)
    # Small robust linear model is the only PyTorch component: no heavy ensemble.
    import torch
    torch.manual_seed(42);torch.set_num_threads(2)
    center=np.median(x[fit],axis=0);scale=np.maximum(np.std(x[fit],axis=0),1)
    tx=torch.tensor((x[fit]-center)/scale,dtype=torch.float32);ty=torch.tensor(y[fit],dtype=torch.float32)
    linear=torch.nn.Linear(x.shape[1],1);optimizer=torch.optim.Adam(linear.parameters(),lr=.25)
    for _ in range(1200):
        optimizer.zero_grad();loss=torch.abs(linear(tx).squeeze(1)-ty).mean()+.001*linear.weight.square().mean();loss.backward();optimizer.step()
    weights=linear.weight.detach().numpy().ravel();bias=float(linear.bias.detach()[0])
    candidates={'persistence':x[:,0],'shrinkage':a*x[:,0]+b,'catboost':cb.predict(x),'torch_lad':((x-center)/scale)@weights+bias}
    scores={k:mae(y[tune],p[tune]) for k,p in candidates.items()}
    selected=min(scores,key=scores.get)
    # Freeze candidate trained only before cut1. Calibration never trains/selects it.
    residuals=y[calibration]-candidates[selected][calibration]
    n=len(residuals);q=float(np.sort(np.abs(residuals))[min(n-1,int(np.ceil((n+1)*.9))-1)])
    meta=dict(selected=selected,features=FEATURES,shrink_a=float(a),shrink_b=float(b),torch_weights=weights.tolist(),torch_bias=bias,center=center.tolist(),scale=scale.tolist(),calibration_residuals=residuals.tolist(),interval_radius_s=q,late_threshold_s=120,nominal_coverage=.9)
    cb.save_model(str(out/'model.cbm'));(out/'model.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
    predictor=Predictor(out);pred=predictor.predict(test_x);truth=test.target_delay_s.to_numpy();fc=predictor.forecast(test_x)
    # Vehicle-block bootstrap quantifies correlated observations more honestly.
    rng=np.random.default_rng(42);groups=[np.flatnonzero(test.tr_id.to_numpy()==k) for k in test.tr_id.unique()]
    boot=[mae(truth[idx],pred[idx]) for idx in [np.concatenate([groups[i] for i in rng.integers(len(groups),size=len(groups))]) for _ in range(1000)]]
    prob=np.array([r['late_probability'] for r in fc]);actual=truth>120
    report=dict(rows_train=len(labels),rows_real=len(real),fit=int(fit.sum()),tune=int(tune.sum()),calibration=n,official_test=len(test),validate=len(val),selected=selected,tuning_mae=scores,
      test_mae_s=mae(truth,pred),zero_mae_s=mae(truth,np.zeros(len(test))),persistence_mae_s=mae(truth,test.cur_dev_s),mae_vehicle_bootstrap_95=np.quantile(boot,[.025,.975]).tolist(),
      interval_coverage=float(np.mean(np.abs(truth-pred)<=q)),interval_radius_s=q,brier_score=float(np.mean((prob-actual)**2)),late_count=int(actual.sum()),
      alert_precision=float(np.sum((prob>=.7)&actual)/max(1,np.sum(prob>=.7))),alert_recall=float(np.sum((prob>=.7)&actual)/max(1,np.sum(actual))),
      chronological_cutoffs=[pd.Timestamp(c,unit='s',tz='UTC').tz_convert('Europe/Moscow').isoformat() for c in [cut1,cut2]],
      limitations=['Only one day; official test is interleaved with train on the same vehicles.', 'Test/validate telemetry is byte-identical. No schedule actuals used as features.', 'Real route IDs and door statuses absent; no route identity or boarding cause can be inferred.', 'Conformal coverage needs exchangeability; measured coverage is reported, not guaranteed.'])
    start=time.perf_counter()
    for _ in range(100): predictor.predict(test_x)
    report['batch_inference_ms']=float((time.perf_counter()-start)*10)
    report['batch_size']=len(test_x)
    (out/'metrics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    submission=pd.DataFrame({'sample_id':val.sample_id,'prediction':predictor.predict(val_x)})
    template=pd.read_csv(root/'sample_submission.csv',sep=';')
    assert set(submission.sample_id)==set(template.sample_id) and submission.sample_id.is_unique
    submission=submission.set_index('sample_id').loc[template.sample_id].reset_index()
    assert np.isfinite(submission.prediction).all()
    submission.to_csv(out/'submission.csv',sep=';',index=False)
    test.assign(prediction=pred,lower=pred-q,upper=pred+q,late_probability=prob).to_csv(out/'test_predictions.csv',index=False)
    train_x.to_csv(out/'train_features.csv',index=False)
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__': main()

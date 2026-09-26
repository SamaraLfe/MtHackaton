"""Reproducible V5 training, calibration, evaluation and submission pipeline."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import catboost
import numpy as np
import pandas as pd
import torch
from catboost import CatBoostRegressor

from ml.v5_features import (
    add_target_coordinates, build_feature_matrix, build_route_features,
    build_telemetry_features, prepare_schedule, prepare_traffic,
)

EXCLUDED_IDENTIFIERS={"tr_id","target_stop_id"}
LATE_THRESHOLD_S=120.0
NOMINAL_COVERAGE=.90


def mae(actual,prediction):
    return float(np.mean(np.abs(np.asarray(actual)-np.asarray(prediction))))


def load_points(path):
    frame=pd.read_csv(path)
    frame["T"]=pd.to_datetime(frame["T"],utc=True)
    frame["target_time_begin"]=pd.to_datetime(frame["target_time_begin"],utc=True)
    frame["tr_id"]=frame["tr_id"].astype(str)
    frame["target_stop_id"]=frame["target_stop_id"].astype(str)
    return frame


def load_traffic(path):
    frame=pd.read_csv(path)
    frame["tr_id"]=frame["tr_id"].astype(str)
    frame["event_time"]=pd.to_datetime(frame["event_time"],utc=True)
    for column,default in (("alt",0.0),("heading",0.0)):
        if column not in frame:frame[column]=default
    return prepare_traffic(frame)


def build_features(points,traffic_path,schedule_path,name):
    traffic=load_traffic(traffic_path)
    schedule=prepare_schedule(pd.read_csv(schedule_path))
    enriched=add_target_coordinates(points,schedule)
    telemetry=build_telemetry_features(enriched,traffic,name)
    route=build_route_features(enriched,schedule,telemetry,name)
    features=build_feature_matrix(enriched,telemetry,route)
    return features.drop(columns=list(EXCLUDED_IDENTIFIERS),errors="ignore")


def conformal_radius(residuals,coverage=NOMINAL_COVERAGE):
    values=np.sort(np.abs(np.asarray(residuals,dtype=float)))
    rank=min(len(values)-1,int(np.ceil((len(values)+1)*coverage))-1)
    return float(values[rank])


def late_probability(predictions,residuals):
    residuals=np.asarray(residuals,dtype=float)
    return np.asarray([(1+np.sum(residuals>LATE_THRESHOLD_S-p))/(len(residuals)+2) for p in predictions],dtype=float)


def torch_baseline(x_fit,y_fit,x_tune):
    medians=np.nanmedian(x_fit,axis=0)
    medians=np.where(np.isfinite(medians),medians,0.0)
    fit=np.where(np.isfinite(x_fit),x_fit,medians)
    tune=np.where(np.isfinite(x_tune),x_tune,medians)
    center=np.median(fit,axis=0);scale=np.maximum(np.std(fit,axis=0),1.0)
    torch.manual_seed(42);torch.set_num_threads(2)
    model=torch.nn.Linear(fit.shape[1],1)
    optimizer=torch.optim.Adam(model.parameters(),lr=.03)
    tx=torch.tensor((fit-center)/scale,dtype=torch.float32)
    ty=torch.tensor(y_fit,dtype=torch.float32)
    for _ in range(400):
        optimizer.zero_grad()
        loss=torch.nn.functional.l1_loss(model(tx).squeeze(1),ty)+.0005*model.weight.square().mean()
        loss.backward();optimizer.step()
    with torch.no_grad():
        return model(torch.tensor((tune-center)/scale,dtype=torch.float32)).squeeze(1).numpy()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--data",default="dataset")
    parser.add_argument("--out",default="artifacts")
    parser.add_argument("--iterations",type=int,default=1200)
    args=parser.parse_args()
    root=Path(args.data);out=Path(args.out);out.mkdir(parents=True,exist_ok=True)

    train=load_points(root/"labels/labels_train.csv")
    train=train[pd.to_numeric(train.tr_id)<9_000_000].sort_values("T").reset_index(drop=True)
    test=load_points(root/"labels/labels_test.csv")
    validate=load_points(root/"validate/points.csv")
    train_x=build_features(train,root/"train/traffic.csv",root/"train/schedule.csv","TRAIN")
    test_x=build_features(test,root/"test/traffic.csv",root/"test/schedule.csv","TEST")
    validate_x=build_features(validate,root/"validate/traffic.csv",root/"validate/schedule_plan.csv","VALIDATE")
    test_x=test_x.reindex(columns=train_x.columns);validate_x=validate_x.reindex(columns=train_x.columns)
    if set(EXCLUDED_IDENTIFIERS)&set(train_x.columns):raise RuntimeError("Raw identifiers leaked into V5 features")

    point_time=train["T"].map(lambda value:value.timestamp())
    target_time=train.target_time_begin.map(lambda value:value.timestamp())
    target=train.target_delay_s.to_numpy(dtype=float)
    available=target_time+np.maximum(target,0)
    cut1=float(point_time.quantile(.65));cut2=float(point_time.quantile(.83))
    fit=available<cut1
    tune=(point_time>=cut1)&(available<cut2)
    calibration=point_time>=cut2
    if min(fit.sum(),tune.sum(),calibration.sum())<20:raise RuntimeError("Chronological split is too small")

    selector=CatBoostRegressor(iterations=args.iterations,depth=6,learning_rate=.03,l2_leaf_reg=8,
        loss_function="MAE",random_seed=42,verbose=False,allow_writing_files=False,thread_count=4)
    selector.fit(train_x[fit],target[fit],eval_set=(train_x[tune],target[tune]),early_stopping_rounds=100)
    best_iterations=max(50,selector.get_best_iteration()+1)
    torch_tune=torch_baseline(train_x[fit].to_numpy(dtype=float),target[fit],train_x[tune].to_numpy(dtype=float))

    development=available<cut2
    model=CatBoostRegressor(iterations=best_iterations,depth=6,learning_rate=.03,l2_leaf_reg=8,
        loss_function="MAE",random_seed=42,verbose=False,allow_writing_files=False,thread_count=4)
    model.fit(train_x[development],target[development])
    calibration_prediction=model.predict(train_x[calibration])
    residuals=target[calibration]-calibration_prediction
    radius=conformal_radius(residuals)

    started=time.perf_counter();test_prediction=model.predict(test_x);batch_ms=(time.perf_counter()-started)*1000
    validate_prediction=model.predict(validate_x)
    truth=test.target_delay_s.to_numpy(dtype=float)
    probability=late_probability(test_prediction,residuals)
    actual_late=truth>LATE_THRESHOLD_S
    coverage=float(np.mean((truth>=test_prediction-radius)&(truth<=test_prediction+radius)))

    feature_names=train_x.columns.tolist()
    metadata={"selected":"v5","version":"v5.1","features":feature_names,
        "calibration_residuals":residuals.tolist(),"interval_radius_s":radius,
        "late_threshold_s":LATE_THRESHOLD_S,"nominal_coverage":NOMINAL_COVERAGE,
        "training":{"split":"purged_chronological","fit_rows":int(development.sum()),
                    "calibration_rows":int(calibration.sum()),"iterations":best_iterations,
                    "identifiers_excluded":sorted(EXCLUDED_IDENTIFIERS)}}
    model.save_model(str(out/"model_v5.cbm"))
    for filename in ("model.json","model_v5.json","v5_feature_schema.json"):
        (out/filename).write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding="utf-8")

    report={"model":"v5.1","rows_train":len(train),"fit":int(development.sum()),
        "tune":int(tune.sum()),"calibration":int(calibration.sum()),"official_test":len(test),
        "validate":len(validate),"features":len(feature_names),"identifiers_excluded":sorted(EXCLUDED_IDENTIFIERS),
        "test_mae_s":mae(truth,test_prediction),"persistence_mae_s":mae(truth,test.cur_dev_s),
        "zero_mae_s":mae(truth,np.zeros(len(test))),"interval_coverage":coverage,
        "interval_radius_s":radius,"brier_score":float(np.mean((probability-actual_late)**2)),
        "alert_precision":float(np.sum((probability>=.7)&actual_late)/max(1,np.sum(probability>=.7))),
        "alert_recall":float(np.sum((probability>=.7)&actual_late)/max(1,np.sum(actual_late))),
        "batch_inference_ms":float(batch_ms),"batch_size":len(test),
        "tuning_mae":{"catboost":mae(target[tune],selector.predict(train_x[tune])),
                      "torch_lad":mae(target[tune],torch_tune),"persistence":mae(target[tune],train.loc[tune,"cur_dev_s"])},
        "limitations":["Only one historical day is available.","Test overlaps train in vehicles and calendar day.",
                       "Door state, official route IDs and labelled incident causes are absent."]}
    (out/"metrics.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")

    test_result=test[["sample_id","tr_id","T","target_stop_id","cur_dev_s","target_delay_s"]].copy()
    test_result["prediction"]=test_prediction;test_result["abs_error"]=np.abs(truth-test_prediction)
    test_result["baseline_abs_error"]=np.abs(truth-test.cur_dev_s.to_numpy(dtype=float))
    test_result["lower_s"]=test_prediction-radius;test_result["upper_s"]=test_prediction+radius
    test_result["late_probability"]=probability
    test_result.to_csv(out/"test_predictions_v5.csv",index=False)

    template=pd.read_csv(root/"sample_submission.csv",sep=";")
    submission=pd.DataFrame({"sample_id":validate.sample_id.astype(str),"prediction":validate_prediction})
    submission=submission.set_index("sample_id").loc[template.sample_id.astype(str)].reset_index()
    for filename in ("submission_v5.csv","submission.csv"):
        submission.to_csv(out/filename,sep=";",index=False)
    pd.DataFrame({"feature":feature_names,"importance":model.get_feature_importance()}).sort_values("importance",ascending=False).to_csv(out/"feature_importance_v5.csv",index=False)
    train_x.assign(target_delay_s=target).to_csv(out/"train_features.csv",index=False)
    (out/"environment.json").write_text(json.dumps({"numpy":np.__version__,"pandas":pd.__version__,
        "catboost":catboost.__version__,"torch":torch.__version__},indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=="__main__":main()

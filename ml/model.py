"""Packaged predictors and empirical uncertainty; no hidden labels are loaded."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from ml.features import FEATURES
from ml.feature_builder import build_v5_row

class Predictor:
    def __init__(self, directory):
        root=Path(directory)
        self.meta=json.loads((root/'model.json').read_text(encoding='utf-8'))
        self.kind=self.meta['selected']
        self.model=None
        if self.kind=='catboost':
            self.model=CatBoostRegressor();self.model.load_model(str(root/'model.cbm'))
        elif self.kind=='v5':
            self.model=CatBoostRegressor();self.model.load_model(str(root/'model.cbm'))
            schema=json.loads((root/'feature_schema.json').read_text(encoding='utf-8'))
            self.v5_features=schema['features']

    def predict(self, frame):
        if self.kind=='v5':
            missing=[name for name in self.v5_features if name not in frame.columns]
            if missing:
                raise ValueError(f'V5 inference requires raw points or a complete {len(self.v5_features)}-feature frame')
            values=frame[self.v5_features].copy()
            for name in ('tr_id','target_stop_id'):
                if name in values.columns: values[name]=values[name].astype(str)
            pred=self.model.predict(values)
            if self.meta.get('target_mode')=='residual_to_current_deviation':
                if 'cur_dev_s' not in frame.columns:
                    raise ValueError('Residual V5 inference requires cur_dev_s')
                pred=pred+frame['cur_dev_s'].to_numpy(dtype=float)
            return np.asarray(pred,dtype=float)
        x=frame[FEATURES].to_numpy(dtype=float)
        if self.kind=='catboost': pred=self.model.predict(x)
        elif self.kind=='torch_lad':
            weights=np.array(self.meta['torch_weights'])
            pred=((x-np.array(self.meta['center']))/np.array(self.meta['scale']))@weights+self.meta['torch_bias']
        elif self.kind=='persistence': pred=x[:,0]
        else: pred=x[:,0]*self.meta['shrink_a']+self.meta['shrink_b']
        return np.asarray(pred,dtype=float)

    def predict_v5(self, points, histories, schedules):
        """Predict V5 from raw prediction points and causal NDTP histories."""
        if self.kind!='v5':
            raise ValueError('Loaded artifact is not a V5 model; retrain with python -m ml.train')
        rows=[build_v5_row(point, history, schedule) for point,history,schedule in zip(points,histories,schedules)]
        values=pd.concat([row[self.v5_features] for row in rows],ignore_index=True)
        prediction=np.asarray(self.model.predict(values),dtype=float)
        if self.meta.get('target_mode')=='residual_to_current_deviation':
            prediction+=np.asarray([float(point['cur_dev_s']) for point in points],dtype=float)
        return prediction

    def forecast_v5(self, points, histories, schedules):
        """Return calibrated V5 point forecasts, intervals and late-risk estimates."""
        predictions=self.predict_v5(points,histories,schedules)
        residuals=np.asarray(self.meta.get('calibration_residuals',[]),dtype=float)
        radius=float(self.meta.get('interval_radius_s',0.0))
        threshold=float(self.meta.get('late_threshold_s',120.0))
        output=[]
        for prediction in predictions:
            probability=None
            if residuals.size:
                probability=float((1+np.sum(residuals>threshold-prediction))/(residuals.size+2))
            output.append({'prediction_s':float(prediction),'lower_s':float(prediction-radius),
                           'upper_s':float(prediction+radius),'late_probability':probability,
                           'model':'v5','uncertainty':'empirical_test_residual'})
        return output

    def forecast(self, frame):
        preds=self.predict(frame);res=np.array(self.meta['calibration_residuals'])
        q=self.meta['interval_radius_s']
        # Empirical residual survival function, Laplace smoothing. This is an
        # approximate risk estimate, not a causal claim or guaranteed probability.
        return [dict(prediction_s=float(p), lower_s=float(p-q), upper_s=float(p+q),
                     late_probability=float((1+np.sum(res>120-p))/(len(res)+2)),
                     model=self.kind) for p in preds]

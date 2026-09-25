"""Models and split-conformal uncertainty; no hidden labels are loaded here."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from ml.features import FEATURES
from ml.v5_features import build_v5_row

class Predictor:
    def __init__(self, directory):
        root=Path(directory)
        self.meta=json.loads((root/'model.json').read_text(encoding='utf-8'))
        self.kind=self.meta['selected']
        self.model=None
        if self.kind=='catboost':
            self.model=CatBoostRegressor();self.model.load_model(str(root/'model.cbm'))
        elif self.kind=='v5':
            self.model=CatBoostRegressor();self.model.load_model(str(root/'model_v5.cbm'))
            schema=json.loads((root/'v5_feature_schema.json').read_text(encoding='utf-8'))
            self.v5_features=schema['features']

    def predict(self, frame):
        if self.kind=='v5':
            missing=[name for name in self.v5_features if name not in frame.columns]
            if missing:
                raise ValueError('V5 inference requires raw points or a complete 104-feature frame')
            values=frame[self.v5_features].copy()
            for name in ('tr_id','target_stop_id'):
                if name in values.columns: values[name]=values[name].astype(str)
            pred=self.model.predict(values)
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
        rows=[build_v5_row(point, history, schedule) for point,history,schedule in zip(points,histories,schedules)]
        values=pd.concat([row[self.v5_features] for row in rows],ignore_index=True)
        for name in ('tr_id','target_stop_id'):
            if name in values.columns: values[name]=values[name].astype(str)
        return np.asarray(self.model.predict(values),dtype=float)

    def forecast(self, frame):
        preds=self.predict(frame);res=np.array(self.meta['calibration_residuals'])
        q=self.meta['interval_radius_s']
        # Empirical residual survival function, Laplace smoothing. This is an
        # approximate risk estimate, not a causal claim or guaranteed probability.
        return [dict(prediction_s=float(p), lower_s=float(p-q), upper_s=float(p+q),
                     late_probability=float((1+np.sum(res>120-p))/(len(res)+2)),
                     model=self.kind) for p in preds]

"""Models and split-conformal uncertainty; no hidden labels are loaded here."""
import json
from pathlib import Path
import numpy as np
from catboost import CatBoostRegressor
from ml.features import FEATURES

class Predictor:
    def __init__(self, directory):
        root=Path(directory)
        self.meta=json.loads((root/'model.json').read_text(encoding='utf-8'))
        self.kind=self.meta['selected']
        self.model=None
        if self.kind=='catboost':
            self.model=CatBoostRegressor();self.model.load_model(str(root/'model.cbm'))

    def predict(self, frame):
        x=frame[FEATURES].to_numpy(dtype=float)
        if self.kind=='catboost': pred=self.model.predict(x)
        elif self.kind=='torch_lad':
            weights=np.array(self.meta['torch_weights'])
            pred=((x-np.array(self.meta['center']))/np.array(self.meta['scale']))@weights+self.meta['torch_bias']
        elif self.kind=='persistence': pred=x[:,0]
        else: pred=x[:,0]*self.meta['shrink_a']+self.meta['shrink_b']
        return np.asarray(pred,dtype=float)

    def forecast(self, frame):
        preds=self.predict(frame);res=np.array(self.meta['calibration_residuals'])
        q=self.meta['interval_radius_s']
        # Empirical residual survival function, Laplace smoothing. This is an
        # approximate risk estimate, not a causal claim or guaranteed probability.
        return [dict(prediction_s=float(p), lower_s=float(p-q), upper_s=float(p+q),
                     late_probability=float((1+np.sum(res>120-p))/(len(res)+2)),
                     model=self.kind) for p in preds]

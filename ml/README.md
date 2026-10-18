# ml/ (R3)

AI4I 2020 dataset (download into `ml/data/`, which is gitignored), `rules.py` (HDF/PWF/OSF/TWF definitions), `train.py` (RandomForest, balanced classes, stratified split, fixed seed), `metrics.json` (rules vs RF: precision, recall, F1, PR-AUC, confusion matrix).

The model takes **AI4I fields only**. Vibration and current go to the rule engine.

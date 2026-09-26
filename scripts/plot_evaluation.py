"""Regenerate the model evaluation figure from versioned V5 predictions."""
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    predictions = pd.read_csv(ROOT / "artifacts" / "test_predictions.csv")
    residuals = predictions["target_delay_s"] - predictions["prediction"]

    figure, axes = plt.subplots(1, 2, figsize=(10, 4), constrained_layout=True)
    axes[0].hist(predictions["abs_error"], bins=24, alpha=0.8, label="V5.3 residual")
    axes[0].hist(predictions["baseline_abs_error"], bins=24, alpha=0.55, label="Persistence")
    axes[0].set(title="Абсолютная ошибка на test", xlabel="секунды", ylabel="наблюдения")
    axes[0].legend()

    axes[1].scatter(predictions["prediction"], residuals, s=12, alpha=0.55)
    axes[1].axhline(0, color="black", linewidth=1)
    axes[1].set(title="Остатки V5.3 residual", xlabel="прогноз, с", ylabel="факт − прогноз, с")

    figure.suptitle("Хронологическая оценка без идентификаторов")
    figure.savefig(ROOT / "docs" / "evaluation.png", dpi=160)
    plt.close(figure)


if __name__ == "__main__":
    main()

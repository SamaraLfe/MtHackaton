"""Regenerate the model evaluation figure from packaged test predictions."""
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    predictions = pd.read_csv(ROOT / "artifacts" / "test_predictions.csv")
    residuals = predictions["actual"] - predictions["prediction"]
    baseline_error = (predictions["actual"] - predictions["cur_dev_s"]).abs()

    figure, axes = plt.subplots(1, 2, figsize=(10, 4), constrained_layout=True)
    axes[0].hist(predictions["abs_error"], bins=24, alpha=0.8, label="champion-b4b")
    axes[0].hist(baseline_error, bins=24, alpha=0.55, label="Persistence")
    axes[0].set(title="Абсолютная ошибка на test", xlabel="секунды", ylabel="наблюдения")
    axes[0].legend()

    axes[1].scatter(predictions["prediction"], residuals, s=12, alpha=0.55)
    axes[1].axhline(0, color="black", linewidth=1)
    axes[1].set(title="Остатки champion-b4b", xlabel="прогноз, с", ylabel="факт − прогноз, с")

    figure.suptitle("Оценка на выданной test-части без идентификаторов")
    figure.savefig(ROOT / "docs" / "evaluation.png", dpi=160)
    plt.close(figure)


if __name__ == "__main__":
    main()

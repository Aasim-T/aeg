"""Small feed-forward classifier for the AEGIS testnet demonstration."""

import math
import random


# Each row specifies (outflow points, market points, allowance points, response).
# These synthetic examples are scenario labels for demonstration.
TRAINING_SCENARIOS = (
    (40, 0, 0, 0), (44, 0, 0, 0), (48, 0, 0, 0), (52, 0, 0, 0), (40, 2, 0, 0),
    (0, 40, 0, 0), (0, 44, 0, 0), (5, 40, 0, 0), (55, 0, 0, 0),
    (20, 0, 20, 1), (26, 0, 20, 1), (36, 0, 20, 1), (20, 20, 0, 1),
    (30, 20, 0, 1), (12, 40, 0, 1), (0, 20, 20, 1), (48, 0, 20, 1),
    (0, 40, 20, 2), (7, 40, 20, 2), (15, 40, 20, 2), (22, 20, 20, 2),
    (35, 20, 20, 2), (27, 40, 0, 2), (32, 40, 0, 2), (53, 20, 0, 2),
)


class RiskModel:
    """Train a three-input, five-hidden-unit classifier from scenario examples."""

    def __init__(self):
        rng = random.Random(42)
        self.hidden = [[rng.uniform(-0.35, 0.35) for _ in range(4)] for _ in range(5)]
        self.output = [[rng.uniform(-0.35, 0.35) for _ in range(6)] for _ in range(3)]

        samples = list(TRAINING_SCENARIOS)
        for _ in range(300):
            rng.shuffle(samples)
            for outflow, market, allowance, label in samples:
                features, hidden, probabilities = self.forward(outflow, market, allowance)
                errors = [probability - (index == label) for index, probability in enumerate(probabilities)]
                hidden_errors = [
                    (1 - hidden[index] ** 2)
                    * sum(error * row[index] for error, row in zip(errors, self.output))
                    for index in range(5)
                ]
                for row, error in zip(self.output, errors):
                    for index, value in enumerate(hidden):
                        row[index] -= 0.08 * error * value
                for row, error in zip(self.hidden, hidden_errors):
                    for index, value in enumerate(features):
                        row[index] -= 0.08 * error * value

    def forward(self, outflow, market, allowance):
        features = (outflow / 80, market / 80, allowance / 20, 1.0)
        hidden = [math.tanh(sum(w * x for w, x in zip(row, features))) for row in self.hidden] + [1.0]
        logits = [sum(w * x for w, x in zip(row, hidden)) for row in self.output]
        weights = [math.exp(logit - max(logits)) for logit in logits]
        return features, hidden, tuple(weight / sum(weights) for weight in weights)

    def recommend(self, outflow, market, allowance):
        score = outflow + market + allowance
        if not 40 <= score < 80:
            return 0
        _, _, probabilities = self.forward(outflow, market, allowance)
        prediction = max(range(3), key=probabilities.__getitem__)
        return min(prediction, 2 if score >= 60 else 1)

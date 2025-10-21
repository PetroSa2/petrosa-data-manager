"""
Machine learning module for anomaly detection and pattern recognition.
"""

from data_manager.ml.statistical_detector import StatisticalAnomalyDetector

# ML-based detectors (optional, require sklearn/tensorflow)
try:
    from data_manager.ml.anomaly_detector import MLAnomalyDetector

    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    MLAnomalyDetector = None

__all__ = [
    "StatisticalAnomalyDetector",
    "MLAnomalyDetector",
    "ML_AVAILABLE",
]

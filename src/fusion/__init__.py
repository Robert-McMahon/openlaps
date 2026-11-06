"""IMU/GNSS sensor fusion: a 15-state error-state inertial navigation filter.

Pure modules, like ``timing``: no clock, no I/O, no catalog. The agent's
``FusionApp`` (``agent/fusion_app.py``) feeds them pre-RBE samples on the
pipeline thread and turns their output into ``fused.*`` derived channels.
See ``docs/FUSION.md`` for the model, frames and tuning.
"""

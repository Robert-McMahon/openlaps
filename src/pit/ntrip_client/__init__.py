"""ntrip-client: fetches RTK corrections at the pit, forwards them vehicle-ward.

See ADR 0006 for the rationale (pit-side NTRIP, RTCM over core NATS,
at-most-once, credentials never on the vehicle).
"""

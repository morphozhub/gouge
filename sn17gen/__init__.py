"""sn17gen: our 404-GEN closed-source miner (port of sn17miner 0.4.0 to the generate(image, seed, witness,
renderer) runtime API; 0.2.0 adds sn17miner v2.2's refine credit cap; 0.2.1 adds the committed service-tier fraction,
default 0.0 = 0.2.0's request bytes; 0.2.2 adds the committed REFINE_RETRY: one refine retry after a recorded
retryable failure or an HTTP-200 stream cut, normal-path request bytes = 0.2.0's; 0.3.0 ports it to 404's published
witness / renderer clients (44d2a64); 0.3.1 makes the witness call work with both client generations, rc6 and
29a0d82, with unchanged request bytes and branches). The committed mode lives in ``sn17gen/settings.py``."""

__version__ = "0.3.1"

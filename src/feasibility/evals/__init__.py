"""Eval harnesses for the two model tasks. With `feasibility.llm`, the only package that may
import the model library. Replay is the default: an eval with no recordings fails with the
library's ReplayMissError and never falls back to a live call."""

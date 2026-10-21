"""Deterministic core of Tiltmeter: shape inference, diffing and classification.

Nothing in this package imports LangChain or LangGraph, so the drift logic can be
tested, reused and reasoned about without the orchestration layer.
"""

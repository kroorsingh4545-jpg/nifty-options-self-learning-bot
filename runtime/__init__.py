"""Composition root.

Wires config, data, broker, risk, execution, strategies and the core engine
together. Nothing in the other packages imports ``runtime`` (except entry
points such as the dashboard), which keeps the dependency rules intact:
``core`` stays import-free and strategies never see broker/execution.
"""

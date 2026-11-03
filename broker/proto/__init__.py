"""Official Upstox market-data feed V3 Protobuf schema.

``MarketDataFeedV3.proto`` is copied verbatim from the official Upstox Python
SDK repository (github.com/upstox/upstox-python). ``MarketDataFeedV3_pb2.py``
is generated from it — do not edit by hand. Regenerate with:

    cd broker/proto
    python -m grpc_tools.protoc -I. --python_out=. --pyi_out=. MarketDataFeedV3.proto
"""

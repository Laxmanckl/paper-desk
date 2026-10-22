"""Volume breakout: the main paper account (since 2026-10-23).

The strategy is ``volume_breakout`` from the Trading-Bot repo, validated there on
16 years of Octa MT5 hourly data (TEST +0.126 R/trade, PF 1.22, random entries
beat it 0.4% of the time; failed only the training-strength and sample-size
criteria). ``lib/`` is a verbatim copy of the modules it needs, so this account
trades exactly the code that was tested. ``runner.py`` feeds it closed hourly
bars from the MT5 bridge and applies the same engine rules the backtest used.
"""

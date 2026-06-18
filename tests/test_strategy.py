import pandas as pd

from app.strategy import calculate_position_shares, evaluate_exit


def test_position_sizing_uses_account_risk():
    shares = calculate_position_shares(
        account_equity=100000,
        entry_price=20,
        stop_loss_pct=0.05,
        risk_per_trade=0.005,
        max_single_position_pct=0.15,
    )
    assert shares == 500


def test_fixed_stop():
    action, _ = evaluate_exit(entry_price=10, current_price=9.49, highest_price=10.2)
    assert action == "SELL"

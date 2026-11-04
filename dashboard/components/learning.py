"""ML / RL model status, learning-loop candidates and pending approvals
(read-only; approvals are given by humans, and LIVE needs explicit approval)."""

from __future__ import annotations

from typing import Any

import pandas as pd


def render(st: Any, models: pd.DataFrame, candidates: pd.DataFrame, approvals: pd.DataFrame) -> None:
    st.subheader("Models and learning loop")
    c = st.columns(3)
    c[0].metric("ML / RL models", len(models), "none trained yet (Phases 7-8)" if models.empty else "",
                delta_color="off", delta_arrow="off")
    c[1].metric("Learning-loop candidates", len(candidates), "loop arrives in Phase 9" if candidates.empty else "",
                delta_color="off", delta_arrow="off")
    c[2].metric("Pending approvals", len(approvals))
    st.markdown("**Model status**")
    st.dataframe(models, hide_index=True, width="stretch", key="ml_models")
    st.markdown("**Learning-loop candidates**")
    st.dataframe(candidates, hide_index=True, width="stretch", key="ml_candidates")
    st.markdown("**Pending approvals** (PAPER first; LIVE only with a named human approval)")
    st.dataframe(approvals, hide_index=True, width="stretch", key="ml_approvals")

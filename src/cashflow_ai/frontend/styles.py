"""Application-wide visual theme for the local Streamlit interface."""

from __future__ import annotations

import streamlit as st

APP_STYLES = """
<style>
    :root {
        --cf-navy: #102a43;
        --cf-navy-soft: #243b53;
        --cf-teal: #0f766e;
        --cf-teal-soft: #e7f6f3;
        --cf-blue-soft: #eaf2ff;
        --cf-amber-soft: #fff7e6;
        --cf-surface: #ffffff;
        --cf-canvas: #f5f7fb;
        --cf-border: #dce4ec;
        --cf-muted: #627386;
    }

    .stApp {
        background:
            radial-gradient(
                circle at 88% 0%,
                rgba(15, 118, 110, 0.08),
                transparent 28rem
            ),
            var(--cf-canvas);
    }

    header[data-testid="stHeader"] {
        background: transparent;
        height: 2.25rem;
    }

    [data-testid="stToolbar"],
    [data-testid="stDecoration"],
    #MainMenu,
    footer {
        display: none !important;
    }

    .block-container {
        max-width: 1180px;
        padding: 2.4rem 2.5rem 5rem;
    }

    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #102a43 0%, #173f4f 100%);
        border-right: 0;
    }

    section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {
        padding: 1.35rem 1rem 1.5rem;
    }

    section[data-testid="stSidebar"] p,
    section[data-testid="stSidebar"] span,
    section[data-testid="stSidebar"] label {
        color: #d9e7ef;
    }

    .cf-brand {
        padding: 0.5rem 0.65rem 1.35rem;
    }

    .cf-brand-mark {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 2.25rem;
        height: 2.25rem;
        margin-right: 0.65rem;
        border-radius: 0.75rem;
        color: #102a43;
        background: #7de2d1;
        font-weight: 800;
    }

    .cf-brand-name {
        color: #ffffff;
        font-size: 1.18rem;
        font-weight: 750;
        letter-spacing: -0.02em;
    }

    .cf-menu-label {
        margin: 0 0.65rem 0.45rem;
        color: #91aaba;
        font-size: 0.68rem;
        font-weight: 750;
        letter-spacing: 0.14em;
    }

    section[data-testid="stSidebar"] div[role="radiogroup"] {
        gap: 0.35rem;
    }

    section[data-testid="stSidebar"] div[role="radiogroup"] label {
        padding: 0.72rem 0.8rem;
        border: 1px solid transparent;
        border-radius: 0.75rem;
        transition: background 120ms ease, border-color 120ms ease;
    }

    section[data-testid="stSidebar"] div[role="radiogroup"] label:hover,
    section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
        background: rgba(255, 255, 255, 0.10);
        border-color: rgba(255, 255, 255, 0.10);
    }

    section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) p {
        color: #ffffff;
        font-weight: 700;
    }

    .cf-sidebar-footer {
        margin: 1.4rem 0.65rem 0;
        padding: 0.85rem;
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 0.8rem;
        color: #bfd0da;
        font-size: 0.78rem;
        line-height: 1.45;
        background: rgba(255, 255, 255, 0.05);
    }

    .cf-page-header {
        margin: 0 0 1.65rem;
        max-width: 780px;
    }

    .cf-eyebrow {
        margin-bottom: 0.5rem;
        color: var(--cf-teal);
        font-size: 0.72rem;
        font-weight: 800;
        letter-spacing: 0.13em;
        text-transform: uppercase;
    }

    .cf-page-title {
        margin: 0;
        color: var(--cf-navy);
        font-size: clamp(1.9rem, 3.5vw, 2.85rem);
        line-height: 1.04;
        letter-spacing: -0.045em;
    }

    .cf-page-description {
        max-width: 700px;
        margin: 0.8rem 0 0;
        color: var(--cf-muted);
        font-size: 1.03rem;
        line-height: 1.65;
    }

    .cf-status {
        display: inline-flex;
        align-items: center;
        gap: 0.55rem;
        margin: 0 0 1.4rem;
        padding: 0.55rem 0.8rem;
        border: 1px solid #bfe6dd;
        border-radius: 999px;
        color: #0b5d56;
        background: var(--cf-teal-soft);
        font-size: 0.82rem;
        font-weight: 700;
    }

    .cf-status-dot {
        width: 0.52rem;
        height: 0.52rem;
        border-radius: 50%;
        background: #18a37f;
        box-shadow: 0 0 0 4px rgba(24, 163, 127, 0.13);
    }

    .cf-status.is-warning {
        border-color: #efd39a;
        color: #805b12;
        background: var(--cf-amber-soft);
    }

    .cf-status.is-warning .cf-status-dot {
        background: #d39a24;
        box-shadow: 0 0 0 4px rgba(211, 154, 36, 0.13);
    }

    .cf-feature-card {
        min-height: 182px;
        padding: 1.25rem;
        border: 1px solid var(--cf-border);
        border-radius: 1rem;
        background: rgba(255, 255, 255, 0.92);
        box-shadow: 0 10px 30px rgba(16, 42, 67, 0.045);
    }

    .cf-feature-icon {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 2.35rem;
        height: 2.35rem;
        margin-bottom: 0.9rem;
        border-radius: 0.75rem;
        background: var(--cf-teal-soft);
        font-size: 1.15rem;
    }

    .cf-feature-card h3 {
        margin: 0 0 0.45rem;
        color: var(--cf-navy);
        font-size: 1rem;
    }

    .cf-feature-card p {
        margin: 0;
        color: var(--cf-muted);
        font-size: 0.88rem;
        line-height: 1.55;
    }

    .cf-notice,
    .cf-empty-state {
        margin: 0.75rem 0 1rem;
        padding: 0.82rem 1rem;
        border: 1px solid var(--cf-border);
        border-left-width: 4px;
        border-radius: 0.75rem;
        background: var(--cf-surface);
        color: var(--cf-navy-soft);
        font-size: 0.86rem;
        line-height: 1.5;
    }

    .cf-notice strong,
    .cf-empty-state strong {
        color: var(--cf-navy);
    }

    .cf-notice.is-private {
        border-left-color: #2a77d4;
        background: var(--cf-blue-soft);
    }

    .cf-notice.is-caution {
        border-left-color: #d39a24;
        background: var(--cf-amber-soft);
    }

    .cf-empty-state {
        padding: 1.2rem;
        border-left-color: var(--cf-teal);
        background: #fbfcfe;
    }

    div[data-testid="stForm"],
    details[data-testid="stExpander"] {
        border: 1px solid var(--cf-border);
        border-radius: 1rem;
        background: rgba(255, 255, 255, 0.78);
    }

    div[data-testid="stFileUploader"] section {
        border: 1.5px dashed #9ab0c1;
        border-radius: 1rem;
        background: #fbfcfe;
    }

    div[data-testid="stMetric"] {
        padding: 1rem 1.05rem;
        border: 1px solid var(--cf-border);
        border-radius: 0.9rem;
        background: var(--cf-surface);
        box-shadow: 0 8px 24px rgba(16, 42, 67, 0.035);
    }

    div[data-testid="stMetric"] [data-testid="stMetricLabel"] p {
        color: var(--cf-muted);
        font-weight: 650;
    }

    .stButton > button,
    .stFormSubmitButton > button,
    .stDownloadButton > button {
        min-height: 2.65rem;
        padding: 0.55rem 1.05rem;
        border-radius: 0.72rem;
        font-weight: 700;
    }

    .stTabs [data-baseweb="tab-list"] {
        gap: 0.4rem;
        overflow-x: auto;
    }

    .stTabs [data-baseweb="tab"] {
        min-width: max-content;
        padding: 0.55rem 0.85rem;
        border-radius: 0.65rem 0.65rem 0 0;
    }

    h1, h2, h3 {
        color: var(--cf-navy);
        letter-spacing: -0.025em;
    }

    hr {
        border-color: var(--cf-border);
    }

    @media (max-width: 700px) {
        .block-container {
            padding: 1.7rem 1rem 4rem;
        }

        .cf-page-title {
            font-size: 1.95rem;
        }

        .cf-feature-card {
            min-height: auto;
            margin-bottom: 0.65rem;
        }
    }
</style>
"""


def apply_app_styles() -> None:
    """Install the static, privacy-safe application stylesheet."""
    st.markdown(APP_STYLES, unsafe_allow_html=True)


__all__ = ["APP_STYLES", "apply_app_styles"]

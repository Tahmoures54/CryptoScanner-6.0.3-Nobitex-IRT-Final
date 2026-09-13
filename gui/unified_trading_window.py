# gui/unified_trading_window.py
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, Optional

from gui.ui_theme import Theme, Styles
from gui.gui_helpers import ConfirmDialog
from trading.execution_mode import LIVE, PAPER, normalize_execution_mode

logger = logging.getLogger(__name__)
T = Theme

class UnifiedTradingWindow(tk.Toplevel):
    """
    Main trading window with tabs for Paper and Real trading.

    Args:
        parent: Parent widget (usually the root window).
        main_app: Reference to the main application instance.
        initial_tab: Name or index of the tab to select initially.
                     Accepts "paper" or "real" (case-insensitive) or integer 0/1.
        use_emojis: If True, tab titles include emojis (default True).
    """

    TAB_PAPER = "paper"
    TAB_REAL = "real"

    def __init__(
        self,
        parent: tk.Widget,
        main_app: Any,
        initial_tab: str | int = "paper",
        use_emojis: bool = True,
        open_settings: bool = False,
    ):
        super().__init__(parent)
        self.main_app = main_app
        self.use_emojis = use_emojis

        # Window configuration
        self.title("SmartEagle Trading Center")
        self.geometry("1200x800")
        self.minsize(1100, 700)
        self.configure(bg=T.BG_APP)

        # Apply ttk styles (should ideally be done once globally,
        # but safe to call here as well)
        style = ttk.Style(self)
        Styles.apply(style)

        self._build_mode_banner()

        # Build notebook and tabs
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=10, pady=(6, 10))

        # Store panels in a dict for easy access and cleanup
        self._panels: Dict[str, tk.Widget] = {}

        # Create tabs
        self._create_tabs()

        # Set initial tab
        self._select_initial_tab(initial_tab)

        # Center window relative to parent
        self._center_window()

        if open_settings:
            panel = self._panels.get(self.TAB_REAL)
            if panel is not None and hasattr(panel, "open_bot_settings"):
                self.after(150, panel.open_bot_settings)

        # Bind close event
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_execution_mode()

    def _build_mode_banner(self) -> None:
        self._mode_bar = tk.Frame(self, bg=T.BG_PANEL, highlightthickness=1, highlightbackground=T.BORDER)
        self._mode_bar.pack(fill="x", padx=10, pady=(10, 0))
        inner = tk.Frame(self._mode_bar, bg=T.BG_PANEL)
        inner.pack(fill="x", padx=12, pady=8)
        self._mode_label = tk.Label(
            inner,
            text="Mode: PAPER",
            font=T.font(size=T.FONT_MD, weight="bold"),
            bg=T.BG_PANEL,
            fg=T.SUCCESS_DARK,
            anchor="w",
        )
        self._mode_label.pack(side="left")
        tk.Button(
            inner,
            text="Use Live",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.DANGER,
            fg=T.TEXT_ON_PRIMARY,
            relief="flat",
            cursor="hand2",
            padx=T.PAD_LG,
            pady=4,
            bd=0,
            command=self._switch_to_live,
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            inner,
            text="Use Paper",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.SUCCESS,
            fg=T.TEXT_ON_PRIMARY,
            relief="flat",
            cursor="hand2",
            padx=T.PAD_LG,
            pady=4,
            bd=0,
            command=self._switch_to_paper,
        ).pack(side="right")

    def refresh_execution_mode(self) -> None:
        mode = normalize_execution_mode(getattr(self.main_app, "execution_mode", PAPER))
        live_on = bool(getattr(self.main_app, "real_auto_enabled", False))
        if mode == LIVE:
            extra = "entries ON" if live_on else "entries paused — press Start on Real tab"
            self._mode_label.config(
                text=f"Mode: LIVE  |  paper stopped  |  {extra}",
                fg=T.DANGER,
            )
        else:
            self._mode_label.config(
                text="Mode: PAPER  |  simulated only  |  no Nobitex orders",
                fg=T.SUCCESS_DARK,
            )
        paper = self._panels.get(self.TAB_PAPER)
        refresh_paper = getattr(paper, "refresh_execution_mode", None)
        if callable(refresh_paper):
            try:
                refresh_paper()
            except Exception:
                pass
        real = self._panels.get(self.TAB_REAL)
        refresh_real = getattr(real, "refresh_execution_mode", None)
        if callable(refresh_real):
            try:
                refresh_real()
            except Exception:
                pass

    def _switch_to_paper(self) -> None:
        setter = getattr(self.main_app, "set_execution_mode", None)
        if callable(setter):
            setter(PAPER, persist=True, reason="trading_window")
        self.refresh_execution_mode()

    def _switch_to_live(self) -> None:
        current = normalize_execution_mode(getattr(self.main_app, "execution_mode", PAPER))
        if current != LIVE:
            ok = ConfirmDialog.ask(
                self,
                title="Switch to Live trading?",
                message="Paper entries will stop. Live orders stay paused until you press Start.",
                detail="Only continue after paper results look acceptable. This uses real money on Nobitex.",
                yes_text="Switch to Live",
                no_text="Stay on Paper",
                danger=True,
            )
            if not ok:
                return
        setter = getattr(self.main_app, "set_execution_mode", None)
        if callable(setter):
            setter(LIVE, persist=True, reason="trading_window")
        self.refresh_execution_mode()
        if self.TAB_REAL in self._panels:
            self._nb.select(self._panels[self.TAB_REAL])

    # -------------------------------------------------------------------------
    # Tab creation
    # -------------------------------------------------------------------------
    def _create_tabs(self) -> None:
        """Create and add the trading panel tabs to the notebook."""
        # Paper Trading tab
        paper_title = "📊 Paper Trading" if self.use_emojis else "Paper Trading"
        try:
            from gui.panels.paper_trading_panel import PaperTradingPanel
            paper_frame = PaperTradingPanel(self._nb, self.main_app)
            self._nb.add(paper_frame, text=paper_title)
            self._panels[self.TAB_PAPER] = paper_frame
        except Exception as e:
            logger.error("Failed to create Paper Trading panel: %s", e, exc_info=True)

        # Real Trading tab
        real_title = "🚀 Real Trading" if self.use_emojis else "Real Trading"
        try:
            from gui.panels.real_trading_panel import RealTradingPanel
            real_frame = RealTradingPanel(self._nb, self.main_app)
            self._nb.add(real_frame, text=real_title)
            self._panels[self.TAB_REAL] = real_frame
        except Exception as e:
            logger.error("Failed to create Real Trading panel: %s", e, exc_info=True)

    def _select_initial_tab(self, initial_tab: str | int) -> None:
        """
        Select the initial tab based on a name or index.

        Accepts:
            - "paper" or 0 -> Paper Trading tab
            - "real" or 1 -> Real Trading tab
        If invalid or tab not found, defaults to Paper Trading.
        """
        # Determine tab key
        if isinstance(initial_tab, int):
            tab_key = self.TAB_PAPER if initial_tab == 0 else self.TAB_REAL
        elif isinstance(initial_tab, str):
            tab_key = initial_tab.lower()
        else:
            tab_key = self.TAB_PAPER  # default

        # Select the tab if it exists
        if tab_key in self._panels:
            tab_id = self._panels[tab_key]
            self._nb.select(tab_id)
        else:
            # Fallback to first tab
            if self._panels:
                first_key = next(iter(self._panels))
                self._nb.select(self._panels[first_key])

    # -------------------------------------------------------------------------
    # Window management
    # -------------------------------------------------------------------------
    def _center_window(self) -> None:
        """Center the window relative to its parent."""
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        parent = self.master
        if parent:
            parent_x = parent.winfo_rootx()
            parent_y = parent.winfo_rooty()
            parent_width = parent.winfo_width()
            parent_height = parent.winfo_height()
            x = parent_x + (parent_width - width) // 2
            y = parent_y + (parent_height - height) // 2
        else:
            # Center on screen if no parent
            screen_width = self.winfo_screenwidth()
            screen_height = self.winfo_screenheight()
            x = (screen_width - width) // 2
            y = (screen_height - height) // 2
        self.geometry(f"+{x}+{y}")

    def _on_close(self) -> None:
        """Handle window close event by cleaning up panels and destroying."""
        logger.debug("Closing UnifiedTradingWindow...")
        for panel in self._panels.values():
            if hasattr(panel, "on_close"):
                try:
                    panel.on_close()
                except Exception as e:
                    logger.error("Error during panel cleanup: %s", e, exc_info=True)
        self.destroy()
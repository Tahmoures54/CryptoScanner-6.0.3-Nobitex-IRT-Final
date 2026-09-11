"""
SettingsWindow — scanner display and market-data API keys only.

Trading size, stops, and Global Lead live in SmartEagle Bot → Settings.
"""
from __future__ import annotations

import logging
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk
import requests

from gui.dialogs.base_dialog import BaseDialog
from gui.dialogs.components import SectionFrame, safe_clipboard_paste, style_button
from gui.gui_helpers import ToolTip
from gui.ui_theme import Theme

logger = logging.getLogger(__name__)
T = Theme

_CMC_API_URL   = "https://api.coinmarketcap.com/api/pricing/"
_CMC_TEST_URL  = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
_CG_TEST_URL   = "https://api.coingecko.com/api/v3/ping"
_BINANCE_TEST_URL = "https://api.binance.com/api/v3/ping"


class SettingsWindow(BaseDialog):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(
            parent, app, title="⚙️ Settings",
            width=640, height=620,
            resizable=(True, True), min_size=(480, 480),
        )
        self._prev_source: str = self.app.api_source_var.get()
        self._testing = False

        self._build_scrollable_body()
        self._build_api_section()
        self._build_bot_pointer_section()
        self._build_display_section()
        self._build_notifications_section()

        self._add_separator()
        self._add_ok_cancel(
            ok_text="💾 Save & Apply",
            cancel_text="Cancel",
            ok_command=self._save_and_close,
        )

    # ──────────────────────────────────────────────────────────────
    # Scrollable body
    # ──────────────────────────────────────────────────────────────
    def _build_scrollable_body(self):
        for widget in self.body.winfo_children():
            widget.destroy()

        self.canvas = tk.Canvas(self.body, bg=T.BG_APP, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self.body, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas, bg=T.BG_APP)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

    def _on_mousewheel(self, event):
        if not hasattr(self, "canvas") or not self.canvas.winfo_exists():
            return
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except tk.TclError:
            pass

    # ──────────────────────────────────────────────────────────────
    # API section
    # ──────────────────────────────────────────────────────────────
    def _build_api_section(self) -> None:
        frame = SectionFrame(self.scrollable_frame, title="API Settings", title_icon="🔌")
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        grid = frame.body
        grid.columnconfigure(1, weight=1)

        tk.Label(grid, text="Data Source:", font=T.font(size=T.FONT_SM),
                 bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
            row=0, column=0, sticky="w", pady=T.PAD_XS)
        self._api_source_cb = ttk.Combobox(
            grid, textvariable=self.app.api_source_var,
            values=["CoinGecko", "CoinMarketCap", "Binance"],
            state="readonly", width=22,
        )
        self._api_source_cb.grid(row=0, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)
        self._api_source_cb.bind("<<ComboboxSelected>>", self._on_source_change)
        ToolTip(self._api_source_cb,
                "CoinGecko: رایگان، بدون کلید\n"
                "CoinMarketCap: نیاز به API Key\n"
                "Binance: دسترسی به کندل‌ها (بدون کلید)")

        tk.Label(grid, text="CoinGecko Key:", font=T.font(size=T.FONT_SM),
                 bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
            row=1, column=0, sticky="w", pady=T.PAD_XS)
        self._cg_key_entry = ttk.Entry(grid, width=36)
        self._cg_key_entry.grid(row=1, column=1, sticky="ew", padx=T.PAD_SM, pady=T.PAD_XS)

        tk.Label(grid, text="CMC API Key:", font=T.font(size=T.FONT_SM),
                 bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
            row=2, column=0, sticky="w", pady=T.PAD_XS)
        self._cmc_key_entry = ttk.Entry(grid, width=36)
        self._cmc_key_entry.grid(row=2, column=1, sticky="ew", padx=T.PAD_SM, pady=T.PAD_XS)

        tk.Label(grid, text="Binance Key:", font=T.font(size=T.FONT_SM),
                 bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
            row=3, column=0, sticky="w", pady=T.PAD_XS)
        self._binance_key_entry = ttk.Entry(grid, width=36)
        self._binance_key_entry.grid(row=3, column=1, sticky="ew", padx=T.PAD_SM, pady=T.PAD_XS)
        if getattr(self.app, "api_key", ""):
            self._cmc_key_entry.insert(0, self.app.api_key)

        btn_row = tk.Frame(grid, bg=T.BG_PANEL)
        btn_row.grid(row=4, column=1, sticky="w", padx=T.PAD_SM, pady=(T.PAD_SM, 0))

        for text, variant, cmd in [
            ("📋 Paste", "primary", self._paste_api_key),
            ("🔑 Get Key", "success", self._open_cmc_page),
            ("🔍 Test", "ghost", self._test_api_key),
        ]:
            btn = tk.Button(btn_row, text=text, command=cmd)
            style_button(btn, variant, padx=T.PAD_MD, pady=T.PAD_XS, bold=False)
            btn.pack(side="left", padx=(0, T.PAD_XS))

        self._test_btn = btn_row.winfo_children()[-1]
        self._test_result_lbl = tk.Label(grid, text="", font=T.font(size=T.FONT_XS),
                                         bg=T.BG_PANEL, fg=T.TEXT_MUTED)
        self._test_result_lbl.grid(row=5, column=1, sticky="w", padx=T.PAD_SM)
        self._on_source_change()

    def _build_bot_pointer_section(self) -> None:
        frame = SectionFrame(self.scrollable_frame, title="Trading Bot", title_icon="🤖")
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        tk.Label(
            frame.body,
            text=(
                "اندازه معامله، حد ضرر، اسکن زنده و استراتژی Global Lead "
                "اینجا نیست — مال ربات است."
            ),
            font=T.font(size=T.FONT_SM),
            bg=T.BG_PANEL,
            fg=T.TEXT_SECONDARY,
            justify="left",
            wraplength=520,
        ).pack(anchor="w", pady=T.PAD_SM)
        btn = tk.Button(
            frame.body,
            text="🤖  SmartEagle Bot  →  ⚙️ Settings",
            command=self._open_bot_settings,
        )
        style_button(btn, "primary", padx=T.PAD_LG, pady=T.PAD_SM, bold=True)
        btn.pack(anchor="w", pady=(0, T.PAD_SM))

    # ──────────────────────────────────────────────────────────────
    # Display & Notifications
    # ──────────────────────────────────────────────────────────────
    def _build_display_section(self) -> None:
        frame = SectionFrame(self.scrollable_frame,
                             title="Display & Behavior", title_icon="🎨")
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        checkboxes = [
            ("🔄 Auto Refresh", self.app.auto_refresh_var,
             "بارگذاری خودکار داده‌ها هر ۵ دقیقه"),
            ("🎯 Simple Mode by Default", self.app.simple_mode_var,
             "نمایش ستون‌های کمتر"),
        ]
        for text, var, tip in checkboxes:
            row = tk.Frame(frame.body, bg=T.BG_PANEL)
            row.pack(fill="x", pady=T.PAD_XS)
            chk = tk.Checkbutton(
                row, text=text, variable=var,
                font=T.font(size=T.FONT_SM),
                bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
                selectcolor=T.PRIMARY_GHOST,
                activebackground=T.BG_PANEL, cursor="hand2",
            )
            chk.pack(side="left")
            ToolTip(chk, tip)

    def _build_notifications_section(self) -> None:
        frame = SectionFrame(self.scrollable_frame,
                             title="Notifications", title_icon="🔔")
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        grid = frame.body
        grid.columnconfigure(1, weight=1)
        tk.Label(grid, text="Email for Alerts:",
                 font=T.font(size=T.FONT_SM),
                 bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
            row=0, column=0, sticky="w", pady=T.PAD_XS)
        self._email_entry = ttk.Entry(
            grid, textvariable=self.app.email_alert_var, width=30
        )
        self._email_entry.grid(row=0, column=1, sticky="ew",
                               padx=T.PAD_SM, pady=T.PAD_XS)

    # ──────────────────────────────────────────────────────────────
    # API helpers (unchanged)
    # ──────────────────────────────────────────────────────────────
    def _on_source_change(self, _=None) -> None:
        source = self.app.api_source_var.get()
        self._cg_key_entry.grid_remove()
        self._cmc_key_entry.grid_remove()
        self._binance_key_entry.grid_remove()
        if source == "CoinGecko":
            self._cg_key_entry.grid()
        elif source == "CoinMarketCap":
            self._cmc_key_entry.grid()
        elif source == "Binance":
            self._binance_key_entry.grid()
        self.app.on_api_source_change()

    def _paste_api_key(self) -> None:
        source = self.app.api_source_var.get()
        entry = (
            self._cmc_key_entry if source == "CoinMarketCap"
            else self._cg_key_entry if source == "CoinGecko"
            else self._binance_key_entry
        )
        text = safe_clipboard_paste(self.dlg)
        if text:
            entry.delete(0, tk.END)
            entry.insert(0, text.strip())

    def _open_cmc_page(self) -> None:
        webbrowser.open(_CMC_API_URL)

    def _test_api_key(self) -> None:
        source = self.app.api_source_var.get()
        if source == "CoinMarketCap":
            key = self._cmc_key_entry.get().strip()
            url = _CMC_TEST_URL
            headers = {"X-CMC_PRO_API_KEY": key}
        elif source == "CoinGecko":
            key = self._cg_key_entry.get().strip()
            url = _CG_TEST_URL
            headers = None
        else:
            key = self._binance_key_entry.get().strip()
            url = _BINANCE_TEST_URL
            headers = None

        if not key and source not in ("CoinGecko", "Binance"):
            self._test_result_lbl.config(text="API key is empty", fg=T.DANGER)
            return

        self._testing = True
        self._test_btn.config(state="disabled")
        self._test_result_lbl.config(text="Testing…", fg=T.INFO)

        def worker():
            def show(text: str, fg: str) -> None:
                self._test_result_lbl.config(text=text, fg=fg)

            try:
                resp = requests.get(url, headers=headers, timeout=10) if headers else requests.get(url, timeout=10)
                if resp.status_code == 200:
                    self._safe_ui_call_from_thread(lambda: show("✅ Success", T.SUCCESS_DARK))
                else:
                    code = resp.status_code
                    self._safe_ui_call_from_thread(lambda: show(f"❌ Error {code}", T.DANGER))
            except Exception as exc:
                msg = str(exc)[:50]
                self._safe_ui_call_from_thread(lambda: show(f"❌ {msg}", T.DANGER))
            finally:
                self._testing = False
                self._safe_ui_call_from_thread(lambda: self._test_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    # ──────────────────────────────────────────────────────────────
    # Save
    # ──────────────────────────────────────────────────────────────
    def _open_bot_settings(self) -> None:
        opener = getattr(self.app, "open_bot_settings", None)
        self.close()
        if callable(opener):
            opener()

    def _save_and_close(self) -> None:
        source = self.app.api_source_var.get()
        if source == "CoinMarketCap":
            new_key = self._cmc_key_entry.get().strip()
        elif source == "CoinGecko":
            new_key = self._cg_key_entry.get().strip()
        else:
            new_key = self._binance_key_entry.get().strip()

        self.app.save_api_key(new_key)
        self.app.save_settings()
        logger.info("Scanner settings saved (API source/display). Bot config is unchanged.")
        self.close()
        try:
            self.app.refresh()
        except Exception as exc:
            logger.warning("Refresh after settings save failed: %s", exc)

    def on_close(self) -> bool:
        if hasattr(self, "canvas") and self.canvas.winfo_exists():
            self.canvas.unbind("<MouseWheel>")
        return True
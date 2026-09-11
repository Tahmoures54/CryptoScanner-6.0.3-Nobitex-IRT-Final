"""
SettingsWindow — پنجره تنظیمات برنامه (v8.0 — User-Friendly Trading Settings)
================================================================================
New in v8.0:
- Redesigned Trading Settings section, prioritized and organized.
- Position Sizing shown FIRST with live preview (in Toman for IRT users).
- Preset buttons: Conservative / Balanced / Aggressive.
- Auto conversion: internally stored in Rial, displayed in Toman for IRT.
- Collapsible "Advanced" section for rarely-touched fields.
- Live preview updates as you type.
"""
from __future__ import annotations

import configparser
import json
import logging
import os
import threading
import webbrowser
from typing import Dict, List, Optional, Tuple
import tkinter as tk
from tkinter import ttk
import requests

from core.config import APPDATA_DIR, CONFIG_FILE
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

TEMPLATE_BOT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "bot_config.json",
)
BOT_CONFIG_PATH = os.path.join(APPDATA_DIR, "bot_config.json")

# در Nobitex، IRT به معنی ریال است. ۱ تومان = ۱۰ ریال.
# برای نمایش به کاربر ایرانی، همه چیز را به تومان نشان می‌دهیم.
TOMAN_PER_RIAL = 0.1


def _load_bot_conf() -> dict:
    for path in (BOT_CONFIG_PATH, TEMPLATE_BOT_CONFIG_PATH):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            logger.warning("Could not load bot_config.json from %s: %s", path, exc)
    return {}


def _is_irt_quote(cfg: dict) -> bool:
    return str(cfg.get("quote_currency", "") or "").upper() in ("IRT", "RLS", "IRR")


def _rial_to_toman(v: float) -> float:
    return float(v) * TOMAN_PER_RIAL


def _toman_to_rial(v: float) -> float:
    return float(v) / TOMAN_PER_RIAL


class SettingsWindow(BaseDialog):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(
            parent, app, title="⚙️ Settings",
            width=720, height=820,
            resizable=(True, True), min_size=(560, 600),
        )
        self._prev_source: str = self.app.api_source_var.get()
        self._testing = False

        # آیا کاربر ایرانی است؟ (برای نمایش تومان)
        self._use_toman = True   # پيش‌فرض برای IRT

        self._bot_conf = _load_bot_conf()

        self._use_toman = _is_irt_quote(self._bot_conf)

        self._build_scrollable_body()

        self._build_api_section()
        self._build_position_sizing_section()   # ← جدید، اول
        self._build_entry_rules_section()       # ← جدید
        self._build_exit_rules_section()        # ← جدید
        self._build_cooldowns_section()         # ← جدید
        self._build_safety_section()            # ← جدید
        self._build_advanced_section()          # ← همه فیلدهای قدیمی
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

    # ──────────────────────────────────────────────────────────────
    # Trading variables store
    # ──────────────────────────────────────────────────────────────
    def _init_trading_vars(self):
        if not hasattr(self, "_trading_vars"):
            self._trading_vars: Dict[str, tk.Variable] = {}

    # ──────────────────────────────────────────────────────────────
    # 💰 POSITION SIZING — First and most important
    # ──────────────────────────────────────────────────────────────
    def _build_position_sizing_section(self) -> None:
        self._init_trading_vars()
        cfg = self._bot_conf

        frame = SectionFrame(
            self.scrollable_frame,
            title="Position Sizing — Position Sizing (اندازه هر معامله)",
            title_icon="💰",
        )
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        grid = frame.body
        grid.columnconfigure(1, weight=1)

        # --- Mode ---
        tk.Label(grid, text="Mode:", font=T.font(size=T.FONT_SM, weight="bold"),
                 bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
            row=0, column=0, sticky="w", pady=T.PAD_XS)
        self._position_size_mode_var = tk.StringVar(
            value=cfg.get("position_size_mode", "fixed")
        )
        combo = ttk.Combobox(
            grid, textvariable=self._position_size_mode_var,
            values=["fixed", "risk_percent"],
            state="readonly", width=22,
        )
        combo.grid(row=0, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)
        combo.bind("<<ComboboxSelected>>", lambda _: self._on_mode_change())
        ToolTip(combo,
                "fixed — مبلغ ثابت برای هر معامله (توصیه می‌شود)\n"
                "risk_percent — محاسبه خودکار بر اساس درصد ریسک")

        # --- Fixed amount (in Toman if IRT) ---
        unit = "تومان" if self._use_toman else "دلار"
        self._fixed_amount_lbl = tk.Label(
            grid,
            text=f"مبلغ ثابت هر معامله ({unit}):",
            font=T.font(size=T.FONT_SM), bg=T.BG_PANEL, fg=T.TEXT_SECONDARY,
        )
        self._fixed_amount_lbl.grid(row=1, column=0, sticky="w", pady=T.PAD_XS)

        raw_fixed = float(cfg.get("fixed_position_quote", 500000.0))
        display_fixed = _rial_to_toman(raw_fixed) if self._use_toman else raw_fixed
        self._fixed_amount_var = tk.StringVar(value=f"{display_fixed:,.0f}")
        self._fixed_amount_entry = ttk.Entry(
            grid, textvariable=self._fixed_amount_var, width=20
        )
        self._fixed_amount_entry.grid(row=1, column=1, sticky="w",
                                      padx=T.PAD_SM, pady=T.PAD_XS)
        self._fixed_amount_var.trace_add("write", lambda *_: self._update_live_preview())

        # --- Risk per trade ---
        self._risk_pct_lbl = tk.Label(
            grid,
            text="درصد ریسک در هر معامله (%):",
            font=T.font(size=T.FONT_SM), bg=T.BG_PANEL, fg=T.TEXT_SECONDARY,
        )
        self._risk_pct_lbl.grid(row=2, column=0, sticky="w", pady=T.PAD_XS)

        self._risk_pct_var = tk.StringVar(value=str(cfg.get("risk_per_trade_pct", 1.0)))
        self._risk_pct_entry = ttk.Entry(
            grid, textvariable=self._risk_pct_var, width=20
        )
        self._risk_pct_entry.grid(row=2, column=1, sticky="w",
                                  padx=T.PAD_SM, pady=T.PAD_XS)
        self._risk_pct_var.trace_add("write", lambda *_: self._update_live_preview())

        # --- Max position % ---
        tk.Label(grid, text="حداکثر درصد سرمایه در هر معامله (%):",
                 font=T.font(size=T.FONT_SM), bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
            row=3, column=0, sticky="w", pady=T.PAD_XS)
        self._max_pos_pct_var = tk.StringVar(value=str(cfg.get("max_position_pct", 10.0)))
        ttk.Entry(grid, textvariable=self._max_pos_pct_var, width=20).grid(
            row=3, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)
        ToolTip(grid.winfo_children()[-1], "سقف ارزش یک معامله نسبت به کل موجودی")

        # --- Max Notional ---
        notional_unit = "تومان" if self._use_toman else "دلار"
        tk.Label(grid, text=f"سقف ارزش هر سفارش ({notional_unit}):",
                 font=T.font(size=T.FONT_SM), bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
            row=4, column=0, sticky="w", pady=T.PAD_XS)
        raw_notional = float(cfg.get("max_notional_quote", 5_000_000.0))
        disp_notional = _rial_to_toman(raw_notional) if self._use_toman else raw_notional
        self._max_notional_var = tk.StringVar(value=f"{disp_notional:,.0f}")
        ttk.Entry(grid, textvariable=self._max_notional_var, width=20).grid(
            row=4, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)

        # --- Max Open Positions ---
        tk.Label(grid, text="حداکثر معاملات هم‌زمان:",
                 font=T.font(size=T.FONT_SM), bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
            row=5, column=0, sticky="w", pady=T.PAD_XS)
        self._max_open_var = tk.StringVar(value=str(cfg.get("max_open_positions", 3)))
        ttk.Entry(grid, textvariable=self._max_open_var, width=20).grid(
            row=5, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)

        # ── Live preview ─────────────────────────────────────────
        preview_box = tk.Frame(grid, bg=T.PRIMARY_GHOST, relief="solid", bd=1)
        preview_box.grid(row=6, column=0, columnspan=2, sticky="ew",
                         padx=T.PAD_SM, pady=(T.PAD_MD, T.PAD_SM))
        self._preview_lbl = tk.Label(
            preview_box,
            text="",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY_GHOST, fg=T.PRIMARY_DARKER,
            justify="right", anchor="e", padx=T.PAD_MD, pady=T.PAD_SM,
        )
        self._preview_lbl.pack(fill="x")

        # ── Preset buttons ────────────────────────────────────────
        preset_row = tk.Frame(grid, bg=T.BG_PANEL)
        preset_row.grid(row=7, column=0, columnspan=2, sticky="w",
                        padx=T.PAD_SM, pady=T.PAD_SM)

        tk.Label(preset_row, text="تنظیمات آماده:",
                 font=T.font(size=T.FONT_XS, weight="bold"),
                 bg=T.BG_PANEL, fg=T.TEXT_MUTED).pack(side="left", padx=(0, T.PAD_SM))

        for text, mode, pct_or_amount, bg in [
            ("🛡️ محافظه‌کار (۵٪)", "risk_percent", 1.0, T.SUCCESS_DARK),
            ("⚖️ متعادل (۱۵٪)", "risk_percent", 3.0, T.PRIMARY),
            ("🚀 تهاجمی (۳۰٪)", "risk_percent", 6.0, T.WARNING),
        ]:
            btn = tk.Button(
                preset_row, text=text,
                font=T.font(size=T.FONT_XS, weight="bold"),
                bg=bg, fg=T.TEXT_ON_PRIMARY,
                activebackground=T.PRIMARY_DARK,
                relief="flat", cursor="hand2",
                padx=T.PAD_MD, pady=T.PAD_XS,
                bd=0,
                command=lambda m=mode, v=pct_or_amount: self._apply_preset(m, v),
            )
            btn.pack(side="left", padx=2)

        self._on_mode_change()

    def _on_mode_change(self):
        """Show/hide the Fixed Amount field based on selected mode."""
        mode = self._position_size_mode_var.get()
        if mode == "fixed":
            self._fixed_amount_lbl.grid()
            self._fixed_amount_entry.grid()
            self._risk_pct_lbl.grid_remove()
            self._risk_pct_entry.grid_remove()
        else:
            self._fixed_amount_lbl.grid_remove()
            self._fixed_amount_entry.grid_remove()
            self._risk_pct_lbl.grid()
            self._risk_pct_entry.grid()
        self._update_live_preview()

    def _apply_preset(self, mode: str, value: float):
        """Apply a preset configuration."""
        self._position_size_mode_var.set(mode)
        if mode == "risk_percent":
            self._risk_pct_var.set(str(value))
            # Set max position % based on preset
            if value <= 1.0:
                self._max_pos_pct_var.set("10.0")
            elif value <= 3.0:
                self._max_pos_pct_var.set("20.0")
            else:
                self._max_pos_pct_var.set("30.0")
        self._on_mode_change()

    def _update_live_preview(self):
        """Update the live preview label."""
        if not hasattr(self, "_preview_lbl"):
            return
        try:
            # Get current balance from the running bot if available, else fallback
            balance_rial = 0.0
            bot = getattr(self.app, "trading_bot", None)
            if bot is not None:
                try:
                    quote = str(
                        self._bot_conf.get("quote_currency", "IRT")
                    ).upper()
                    bal = bot.get_balance(quote)
                    if bal is not None and bal > 0:
                        balance_rial = float(bal)
                except Exception:
                    pass
            if balance_rial <= 0:
                balance_rial = float(self._bot_conf.get("account_balance", 0) or 0)

            mode = self._position_size_mode_var.get()
            max_open = int(float(self._max_open_var.get() or 3))
            max_pos_pct = float(self._max_pos_pct_var.get() or 10.0)

            if mode == "fixed":
                try:
                    disp = float(self._fixed_amount_var.get().replace(",", ""))
                except ValueError:
                    disp = 0.0
                per_trade_rial = _toman_to_rial(disp) if self._use_toman else disp
            else:
                try:
                    risk_pct = float(self._risk_pct_var.get())
                except ValueError:
                    risk_pct = 1.0
                sl_pct = float(
                    self._trading_vars.get("stop_loss_pct",
                                           tk.StringVar(value="3")).get() or 3.0
                )
                per_trade_rial = (balance_rial * risk_pct / 100.0) / (sl_pct / 100.0) if sl_pct > 0 else 0.0

            # Apply max_position_pct cap
            cap_rial = balance_rial * max_pos_pct / 100.0
            per_trade_rial = min(per_trade_rial, cap_rial)

            # Format
            if self._use_toman:
                per_trade_disp = _rial_to_toman(per_trade_rial)
                balance_disp = _rial_to_toman(balance_rial)
                unit = "تومان"
            else:
                per_trade_disp = per_trade_rial
                balance_disp = balance_rial
                unit = "$"

            pct = (per_trade_rial / balance_rial * 100.0) if balance_rial > 0 else 0.0
            total = per_trade_disp * max_open

            msg = (
                f"📊 پیش‌نمایش:  هر معامله ≈ {per_trade_disp:,.0f} {unit} "
                f"({pct:.1f}٪ از حساب)\n"
                f"🔢 حداکثر {max_open} معامله هم‌زمان ≈ {total:,.0f} {unit}\n"
                f"💰 موجودی: {balance_disp:,.0f} {unit}"
            )
            self._preview_lbl.config(text=msg)
        except Exception as e:
            logger.debug("Preview update failed: %s", e)

    # ──────────────────────────────────────────────────────────────
    # 🎯 ENTRY RULES
    # ──────────────────────────────────────────────────────────────
    def _build_entry_rules_section(self) -> None:
        self._init_trading_vars()
        cfg = self._bot_conf

        frame = SectionFrame(self.scrollable_frame,
                             title="Entry Rules — قوانین ورود", title_icon="🎯")
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        grid = frame.body
        grid.columnconfigure(1, weight=1)

        fields = [
            ("pump_threshold_pct", "آستانه حرکت قیمت (%)",
             "حداقل رشد قیمت برای ورود (پیش‌فرض 3.0)", 3.0),
            ("movement_lookback_scans", "تعداد اسکن برای محاسبه حرکت",
             "چند اسکن قبلی مبنای حرکت واقعی CMC باشد (پیش‌فرض 6)", 6),
            ("min_volume_24h", "حداقل حجم ۲۴ ساعته ($)",
             "توکن‌های بی‌نقدینگی رد شوند (پیشنهاد: 500000)", 500000.0),
        ]

        for i, (key, label, tip, default) in enumerate(fields):
            tk.Label(grid, text=label, font=T.font(size=T.FONT_SM),
                     bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
                row=i, column=0, sticky="w", pady=T.PAD_XS)
            var = tk.StringVar(value=str(cfg.get(key, default)))
            entry = ttk.Entry(grid, textvariable=var, width=20)
            entry.grid(row=i, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)
            ToolTip(entry, tip)
            self._trading_vars[key] = var
            if key == "stop_loss_pct":
                var.trace_add("write", lambda *_: self._update_live_preview())

    # ──────────────────────────────────────────────────────────────
    # 🛡️ EXIT RULES
    # ──────────────────────────────────────────────────────────────
    def _build_exit_rules_section(self) -> None:
        self._init_trading_vars()
        cfg = self._bot_conf

        frame = SectionFrame(self.scrollable_frame,
                             title="Exit Rules — قوانین خروج", title_icon="🛡️")
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        grid = frame.body
        grid.columnconfigure(1, weight=1)

        fields = [
            ("stop_loss_pct", "حد ضرر (%)",
             "چند درصد زیر قیمت خرید بفروشد", 3.0),
            ("trailing_distance_pct", "فاصله Trailing Stop (%)",
             "فاصله استاپ متحرک از بالاترین قیمت", 2.0),
            ("trailing_activation_pct", "فعال‌سازی Trailing (%)",
             "از چند درصد سود، Trailing فعال شود", 2.0),
            ("take_profit_percent", "حد سود (%)",
             "0 = غیرفعال (فقط Trailing و SL)", 0.0),
        ]

        for i, (key, label, tip, default) in enumerate(fields):
            tk.Label(grid, text=label, font=T.font(size=T.FONT_SM),
                     bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
                row=i, column=0, sticky="w", pady=T.PAD_XS)
            var = tk.StringVar(value=str(cfg.get(key, default)))
            entry = ttk.Entry(grid, textvariable=var, width=20)
            entry.grid(row=i, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)
            ToolTip(entry, tip)
            self._trading_vars[key] = var
            if key == "stop_loss_pct":
                var.trace_add("write", lambda *_: self._update_live_preview())

        row = len(fields)
        self._trailing_enabled_var = tk.BooleanVar(
            value=bool(cfg.get("trailing_stop_enabled", True))
        )
        chk = tk.Checkbutton(
            grid,
            text="✅ Trailing Stop فعال باشد",
            variable=self._trailing_enabled_var,
            font=T.font(size=T.FONT_SM),
            bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
            selectcolor=T.PRIMARY_GHOST,
            activebackground=T.BG_PANEL, cursor="hand2",
        )
        chk.grid(row=row, column=0, columnspan=2, sticky="w", pady=T.PAD_XS)
        self._trading_vars["trailing_stop_enabled"] = self._trailing_enabled_var

    # ──────────────────────────────────────────────────────────────
    # ⏱️ COOLDOWNS
    # ──────────────────────────────────────────────────────────────
    def _build_cooldowns_section(self) -> None:
        self._init_trading_vars()
        cfg = self._bot_conf

        frame = SectionFrame(self.scrollable_frame,
                             title="Cooldowns — زمان انتظار بین معاملات",
                             title_icon="⏱️")
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        grid = frame.body
        grid.columnconfigure(1, weight=1)

        fields = [
            ("cooldown_after_loss_min", "انتظار پس از ضرر (دقیقه)", 15),
            ("cooldown_after_win_min", "انتظار پس از سود (دقیقه)", 5),
        ]

        for i, (key, label, default) in enumerate(fields):
            tk.Label(grid, text=label, font=T.font(size=T.FONT_SM),
                     bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
                row=i, column=0, sticky="w", pady=T.PAD_XS)
            var = tk.StringVar(value=str(cfg.get(key, default)))
            ttk.Entry(grid, textvariable=var, width=20).grid(
                row=i, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)
            self._trading_vars[key] = var

    # ──────────────────────────────────────────────────────────────
    # 🔒 SAFETY
    # ──────────────────────────────────────────────────────────────
    def _build_safety_section(self) -> None:
        self._init_trading_vars()
        cfg = self._bot_conf

        frame = SectionFrame(self.scrollable_frame,
                             title="Safety — حفاظت سرمایه", title_icon="🔒")
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        grid = frame.body
        grid.columnconfigure(1, weight=1)

        tk.Label(grid, text="حداکثر افت سرمایه قبل از توقف (%):",
                 font=T.font(size=T.FONT_SM), bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
            row=0, column=0, sticky="w", pady=T.PAD_XS)
        var = tk.StringVar(value=str(cfg.get("max_drawdown_percent", 10.0)))
        ttk.Entry(grid, textvariable=var, width=20).grid(
            row=0, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)
        self._trading_vars["max_drawdown_percent"] = var

        self._halt_on_dd_var = tk.BooleanVar(
            value=bool(cfg.get("halt_on_max_drawdown", True))
        )
        tk.Checkbutton(
            grid, text="✅ در صورت افت بیش از حد، معاملات متوقف شود",
            variable=self._halt_on_dd_var,
            font=T.font(size=T.FONT_SM),
            bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
            selectcolor=T.PRIMARY_GHOST,
            activebackground=T.BG_PANEL, cursor="hand2",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=T.PAD_XS)
        self._trading_vars["halt_on_max_drawdown"] = self._halt_on_dd_var

        self._use_risk_filter_var = tk.BooleanVar(
            value=bool(cfg.get("use_risk_filter", False))
        )
        tk.Checkbutton(
            grid, text="✅ فیلتر ریسک (رد کردن High/Extreme)",
            variable=self._use_risk_filter_var,
            font=T.font(size=T.FONT_SM),
            bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
            selectcolor=T.PRIMARY_GHOST,
            activebackground=T.BG_PANEL, cursor="hand2",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=T.PAD_XS)
        self._trading_vars["use_risk_filter"] = self._use_risk_filter_var

        tk.Label(grid, text="سطوح ریسک مسدود (با کاما):",
                 font=T.font(size=T.FONT_SM), bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
            row=3, column=0, sticky="w", pady=T.PAD_XS)
        self._blocked_risk_var = tk.StringVar(
            value=", ".join(cfg.get("blocked_risk_levels", ["High", "Extreme"]))
        )
        ttk.Entry(grid, textvariable=self._blocked_risk_var, width=30).grid(
            row=3, column=1, sticky="ew", padx=T.PAD_SM, pady=T.PAD_XS)

    # ──────────────────────────────────────────────────────────────
    # ⚙️ ADVANCED (collapsible)
    # ──────────────────────────────────────────────────────────────
    def _build_advanced_section(self) -> None:
        self._init_trading_vars()
        cfg = self._bot_conf

        outer = tk.Frame(self.scrollable_frame, bg=T.BG_PANEL,
                         highlightthickness=1, highlightbackground=T.BORDER)
        outer.pack(fill="x", pady=(0, T.PAD_MD))

        header = tk.Frame(outer, bg=T.PRIMARY_GHOST)
        header.pack(fill="x")

        self._advanced_open = tk.BooleanVar(value=False)
        self._advanced_arrow = tk.Label(
            header, text="▶", font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY_GHOST, fg=T.PRIMARY_DARK, cursor="hand2",
        )
        self._advanced_arrow.pack(side="left", padx=(T.PAD_MD, 4), pady=T.PAD_SM)
        title_lbl = tk.Label(
            header, text="⚙️  Advanced Settings (تنظیمات پیشرفته)",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY_GHOST, fg=T.PRIMARY_DARK, cursor="hand2",
        )
        title_lbl.pack(side="left", pady=T.PAD_SM)

        self._advanced_body = tk.Frame(outer, bg=T.BG_PANEL)
        grid = self._advanced_body
        grid.columnconfigure(1, weight=1)

        fields = [
            ("min_market_cap", "Min Market Cap ($)", 0.0),
            ("min_quality", "Min Quality (0-1)", 0.4),
            ("min_notional_quote", "Min Notional per Order", 100000.0),
            ("max_total_exposure_pct", "Max Total Exposure (%)", 30.0),
            ("entry_cooldown_seconds", "Entry Cooldown (sec)", 300),
            ("trading_fee_pct", "Trading Fee (%)", 0.1),
            ("max_new_entries_per_cycle", "Max New Entries per Cycle", 2),
            ("confirmation_pct", "Confirmation (%)", 0.5),
            ("confirmation_max_minutes", "Confirmation Max Minutes", 45),
            ("invalidation_pct", "Invalidation (%)", 1.5),
            ("max_chase_pct", "Max Chase (%)", 1.0),
            ("account_balance", "Account Balance (quote)", 10000000.0),
            ("kline_limit", "Kline Limit", 100),
            ("check_interval_seconds", "Check Interval (sec)", 15),
            ("global_pump_threshold_pct", "CMC / observed lead threshold (%)", 1.8),
            ("min_nobitex_discount_pct", "Min Nobitex discount vs CMC (%)", 0.4),
            ("max_nobitex_spread_pct", "Max Nobitex spread (%)", 2.2),
            ("min_global_volume_usd", "Min CMC 24h volume (USD)", 300000.0),
            ("max_global_quote_age_sec", "Max CMC quote age (sec)", 240.0),
            ("min_observed_move_pct", "Min observed CMC move (%)", 0.6),
            ("max_local_premium_pct", "Allow Nobitex premium if local lags (%)", 1.5),
            ("max_local_24h_pct", "Max local 24h already-pumped (%)", 16.0),
            ("btc_max_dump_pct", "Skip alts if BTC dumps more than (%)", 1.5),
        ]

        for i, (key, label, default) in enumerate(fields):
            tk.Label(grid, text=label + ":", font=T.font(size=T.FONT_SM),
                     bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
                row=i, column=0, sticky="w", pady=2, padx=T.PAD_SM)
            var = tk.StringVar(value=str(cfg.get(key, default)))
            ttk.Entry(grid, textvariable=var, width=22).grid(
                row=i, column=1, sticky="w", padx=T.PAD_SM, pady=2)
            self._trading_vars[key] = var

        row = len(fields)
        self._enable_auto_trading_var = tk.BooleanVar(
            value=bool(cfg.get("enable_auto_trading", True))
        )
        tk.Checkbutton(
            grid, text="Enable Auto Trading",
            variable=self._enable_auto_trading_var,
            font=T.font(size=T.FONT_SM),
            bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
            selectcolor=T.PRIMARY_GHOST,
            activebackground=T.BG_PANEL, cursor="hand2",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2, padx=T.PAD_SM)
        self._trading_vars["enable_auto_trading"] = self._enable_auto_trading_var
        row += 1

        self._reverse_signal_exit_var = tk.BooleanVar(
            value=bool(cfg.get("reverse_signal_exit_enabled", True))
        )
        tk.Checkbutton(
            grid, text="Reverse Signal Exit",
            variable=self._reverse_signal_exit_var,
            font=T.font(size=T.FONT_SM),
            bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
            selectcolor=T.PRIMARY_GHOST,
            activebackground=T.BG_PANEL, cursor="hand2",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2, padx=T.PAD_SM)
        self._trading_vars["reverse_signal_exit_enabled"] = self._reverse_signal_exit_var
        row += 1

        self._confirmation_enabled_var = tk.BooleanVar(
            value=bool(cfg.get("confirmation_enabled", False))
        )
        tk.Checkbutton(
            grid, text="Enable Confirmation",
            variable=self._confirmation_enabled_var,
            font=T.font(size=T.FONT_SM),
            bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
            selectcolor=T.PRIMARY_GHOST,
            activebackground=T.BG_PANEL, cursor="hand2",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2, padx=T.PAD_SM)
        self._trading_vars["confirmation_enabled"] = self._confirmation_enabled_var

        def toggle(event=None):
            if self._advanced_open.get():
                self._advanced_body.pack_forget()
                self._advanced_arrow.config(text="▶")
                self._advanced_open.set(False)
            else:
                self._advanced_body.pack(fill="x", padx=T.PAD_MD, pady=T.PAD_MD)
                self._advanced_arrow.config(text="▼")
                self._advanced_open.set(True)

        self._advanced_arrow.bind("<Button-1>", toggle)
        title_lbl.bind("<Button-1>", toggle)

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
    def _save_and_close(self) -> None:
        # 1) ذخیره API key
        source = self.app.api_source_var.get()
        if source == "CoinMarketCap":
            new_key = self._cmc_key_entry.get().strip()
        elif source == "CoinGecko":
            new_key = self._cg_key_entry.get().strip()
        else:
            new_key = self._binance_key_entry.get().strip()

        self.app.save_api_key(new_key)
        self.app.save_settings()

        # 2) بارگذاری مجدد bot_config.json از AppData (مسیر واقعی ربات)
        try:
            os.makedirs(os.path.dirname(BOT_CONFIG_PATH), exist_ok=True)
            with open(BOT_CONFIG_PATH, "r", encoding="utf-8") as f:
                bot_conf = json.load(f)
        except Exception:
            bot_conf = dict(self._bot_conf)

        # 3) Position sizing fields
        try:
            bot_conf["position_size_mode"] = self._position_size_mode_var.get()
        except Exception:
            pass

        # Fixed amount (Toman → Rial)
        try:
            raw_disp = float(str(self._fixed_amount_var.get()).replace(",", "").strip() or "0")
            bot_conf["fixed_position_quote"] = (
                _toman_to_rial(raw_disp) if self._use_toman else raw_disp
            )
        except (ValueError, AttributeError) as e:
            logger.warning("Invalid fixed_position_quote: %s", e)

        # Max Notional (Toman → Rial)
        try:
            raw_notional_disp = float(str(self._max_notional_var.get()).replace(",", "").strip() or "0")
            bot_conf["max_notional_quote"] = (
                _toman_to_rial(raw_notional_disp) if self._use_toman else raw_notional_disp
            )
        except (ValueError, AttributeError) as e:
            logger.warning("Invalid max_notional_quote: %s", e)

        # 4) All other numeric fields from _trading_vars
        int_keys = {
            "movement_lookback_scans", "max_open_positions",
            "max_new_entries_per_cycle", "cooldown_after_loss_min",
            "cooldown_after_win_min", "entry_cooldown_seconds",
            "confirmation_max_minutes", "kline_limit",
            "check_interval_seconds", "min_confirm_scans",
        }
        skip_keys = {
            "fixed_position_quote", "max_notional_quote",
            "position_size_mode", "blocked_risk_levels",
        }

        for key, var in self._trading_vars.items():
            if key in skip_keys:
                continue
            try:
                if isinstance(var, tk.BooleanVar):
                    bot_conf[key] = bool(var.get())
                else:
                    raw = str(var.get()).replace(",", "").strip()
                    if not raw:
                        continue
                    if key in int_keys:
                        bot_conf[key] = int(float(raw))
                    else:
                        bot_conf[key] = float(raw)
            except (ValueError, TypeError) as e:
                logger.warning("Invalid value for %s: %s", key, e)

        # 5) blocked_risk_levels
        try:
            levels = [
                x.strip() for x in self._blocked_risk_var.get().split(",")
                if x.strip()
            ]
            bot_conf["blocked_risk_levels"] = levels
        except Exception:
            pass

        # 6) Also set max_open_trades (tracker reads both)
        try:
            if "max_open_positions" in bot_conf:
                bot_conf["max_open_trades"] = int(bot_conf["max_open_positions"])
        except Exception:
            pass

        bot_conf["strategy"] = "global_lead_local_lag"
        bot_conf["global_signal_source"] = "CoinMarketCap"

        # 7) Save file to the live AppData config the executor actually reads
        try:
            os.makedirs(os.path.dirname(BOT_CONFIG_PATH), exist_ok=True)
            with open(BOT_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(bot_conf, f, indent=4, ensure_ascii=False)
            logger.info("Settings saved to %s", BOT_CONFIG_PATH)
        except Exception as e:
            logger.error("Failed to save bot_config.json: %s", e)

        self.close()

        # 8) Apply to running paper + live trackers
        apply_keys = (
            "pump_threshold_pct", "stop_loss_pct",
            "trailing_distance_pct", "risk_per_trade_pct",
            "max_open_trades", "take_profit_percent",
            "max_drawdown_percent", "fixed_position_quote",
            "position_size_mode", "max_position_pct",
            "max_notional_quote", "min_notional_quote", "min_volume_24h",
            "min_market_cap", "max_new_entries_per_cycle",
            "cooldown_after_loss_min", "cooldown_after_win_min",
            "max_total_exposure_pct", "entry_cooldown_seconds",
            "confirmation_enabled", "confirmation_pct",
            "confirmation_max_minutes", "invalidation_pct", "max_chase_pct",
        )
        try:
            for attr in ("signal_tracker", "real_signal_tracker"):
                st = getattr(self.app, attr, None)
                if st is None:
                    continue
                for key in apply_keys:
                    if key in bot_conf:
                        try:
                            setattr(st, key, bot_conf[key])
                        except Exception:
                            pass
                if hasattr(st, "max_open_trades") and "max_open_positions" in bot_conf:
                    st.max_open_trades = int(bot_conf["max_open_positions"])
                if hasattr(st, "_save_state"):
                    st._save_state()
        except Exception as exc:
            logger.warning("Could not hot-apply settings: %s", exc)

        try:
            from trading.bot_config import load_config
            cfg = load_config(BOT_CONFIG_PATH)
            self.app._bot_cfg = cfg
            if hasattr(self.app, "_configure_global_lead_engine"):
                self.app._configure_global_lead_engine(cfg)
        except Exception as exc:
            logger.warning("Could not rebuild live lead engine: %s", exc)

        try:
            self.app.refresh()
        except Exception as exc:
            logger.warning("Refresh after settings save failed: %s", exc)

    def on_close(self) -> bool:
        if hasattr(self, "canvas") and self.canvas.winfo_exists():
            self.canvas.unbind("<MouseWheel>")
        return True
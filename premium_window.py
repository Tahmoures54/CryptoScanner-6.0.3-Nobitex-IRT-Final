from __future__ import annotations  # باید اول باشد

import tkinter as tk
from tkinter import ttk, messagebox
import json, base64, hmac, hashlib, secrets, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

KEY_FILE = Path("secrets/license_key.key")


def load_or_create_secret_key(key_path: Path = KEY_FILE) -> bytes:
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        key = key_path.read_bytes()
        if len(key) >= 32:
            return key[:32]
        else:
            raise ValueError(f"Key file {key_path} is shorter than 32 bytes")
    key = secrets.token_bytes(32)
    key_path.write_bytes(key)
    print(f"New secret key created at {key_path}")
    return key


def base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def base64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def generate_activation_code(
    plan_months: int,
    device_id: Optional[str] = None,
    valid_days: Optional[int] = None,
    secret_key: Optional[bytes] = None
) -> str:
    if secret_key is None:
        secret_key = load_or_create_secret_key()

    payload = {
        "plan": f"{plan_months}months",
        "device_id": device_id,
        "issued_at": int(time.time()),
    }

    if valid_days is not None:
        expires = datetime.now(timezone.utc) + timedelta(days=valid_days)
        payload["expires_at"] = int(expires.timestamp())
    else:
        payload["expires_at"] = None

    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_b64 = base64url_encode(payload_json.encode("utf-8"))
    signature = hmac.new(secret_key, payload_b64.encode("utf-8"), hashlib.sha256)
    signature_b64 = base64url_encode(signature.digest())
    return f"{payload_b64}.{signature_b64}"


def verify_activation_code(
    code: str,
    secret_key: Optional[bytes] = None,
    device_id: Optional[str] = None
) -> tuple[bool, str, dict]:
    if secret_key is None:
        secret_key = load_or_create_secret_key()

    try:
        payload_b64, signature_b64 = code.split(".", 1)
    except ValueError:
        return False, "Invalid code format", {}

    expected_sig = hmac.new(secret_key, payload_b64.encode("utf-8"), hashlib.sha256)
    expected_sig_b64 = base64url_encode(expected_sig.digest())
    if not hmac.compare_digest(signature_b64, expected_sig_b64):
        return False, "Invalid signature", {}

    try:
        payload_json = base64url_decode(payload_b64).decode("utf-8")
        payload = json.loads(payload_json)
    except Exception:
        return False, "Cannot read payload", {}

    expires_at = payload.get("expires_at")
    if expires_at is not None and int(time.time()) > int(expires_at):
        return False, "Code has expired", {}

    bound_device = payload.get("device_id")
    if bound_device is not None and (device_id is None or bound_device != device_id):
        return False, "Code is bound to another device", {}

    return True, "Valid code", payload


class ActivationCodeGeneratorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Activation Code Generator")
        self.root.geometry("600x500")
        self.root.resizable(False, False)

        self.secret_key = self._init_key()
        self._create_widgets()

    def _init_key(self) -> bytes:
        try:
            return load_or_create_secret_key()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load/create key: {e}")
            self.root.destroy()
            raise SystemExit  # خروج کامل از برنامه

    def _create_widgets(self) -> None:
        main_frame = ttk.Frame(self.root, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Plan Months
        ttk.Label(main_frame, text="Plan Months:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.months_var = tk.IntVar(value=1)
        months_spin = ttk.Spinbox(
            main_frame,
            from_=1,
            to=120,
            textvariable=self.months_var,
            width=10
        )
        months_spin.grid(row=0, column=1, sticky=tk.W, pady=5)

        # Device ID
        ttk.Label(main_frame, text="Device ID (optional):").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.device_id_var = tk.StringVar()
        device_entry = ttk.Entry(main_frame, textvariable=self.device_id_var, width=40)
        device_entry.grid(row=1, column=1, sticky=tk.W, pady=5)

        # Valid Days
        ttk.Label(main_frame, text="Code Valid Days (optional):").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.valid_days_var = tk.StringVar()
        valid_days_entry = ttk.Entry(main_frame, textvariable=self.valid_days_var, width=10)
        valid_days_entry.grid(row=2, column=1, sticky=tk.W, pady=5)

        # Generate Button
        generate_btn = ttk.Button(
            main_frame,
            text="Generate Activation Code",
            command=self.generate_code
        )
        generate_btn.grid(row=3, column=0, columnspan=2, pady=20)

        # Output Label
        ttk.Label(main_frame, text="Activation Code:").grid(row=4, column=0, sticky=tk.W)
        self.output_text = tk.Text(main_frame, height=5, width=60, wrap=tk.WORD)
        self.output_text.grid(row=5, column=0, columnspan=2, pady=5)

        # Copy Button
        copy_btn = ttk.Button(main_frame, text="Copy to Clipboard", command=self.copy_code)
        copy_btn.grid(row=6, column=0, columnspan=2, pady=5)

        # Status Label
        self.status_var = tk.StringVar(value="Ready")
        status_label = ttk.Label(main_frame, textvariable=self.status_var, foreground="green")
        status_label.grid(row=7, column=0, columnspan=2)

    def generate_code(self) -> None:
        try:
            # Validate months
            months = int(self.months_var.get())
            if months < 1:
                raise ValueError("Months must be at least 1")

            # Device ID
            device_id = self.device_id_var.get().strip()
            if not device_id:
                device_id = None

            # Valid days
            valid_days_str = self.valid_days_var.get().strip()
            valid_days = int(valid_days_str) if valid_days_str else None
            if valid_days is not None and valid_days < 1:
                raise ValueError("Valid days must be at least 1")

            code = generate_activation_code(months, device_id, valid_days, self.secret_key)
            self.output_text.delete(1.0, tk.END)
            self.output_text.insert(tk.END, code)
            self.status_var.set("Code generated successfully.")

        except ValueError as ve:
            messagebox.showerror("Invalid Input", str(ve))
            self.status_var.set("Error: invalid input")
        except Exception as e:
            messagebox.showerror("Error", str(e))
            self.status_var.set("Error generating code")

    def copy_code(self) -> None:
        code = self.output_text.get(1.0, tk.END).strip()
        if code:
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.status_var.set("Copied to clipboard.")
        else:
            messagebox.showwarning("No Code", "Generate a code first.")


if __name__ == "__main__":
    root = tk.Tk()
    app = ActivationCodeGeneratorApp(root)
    root.mainloop()
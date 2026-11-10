"""
gui.py - Tkinter front end for Wire Diagram Builder.

Tabs
  1. Review    - load a Sales Order / Quote, sort it into "Relevant for diagram"
                 and "Not part of diagram", move/map lines, add customer-supplied
                 or custom devices, pick pipettors, apply DRD §7 pairing, and
                 optionally check the order against a CPR document.
  2. Analysis  - options (15A/20A, main PC, HHS method, VANTAGE isolation,
                 over-80% approval), run, pin loads to circuits, blocking
                 issues, export the Power Distribution CSV / report.
  3. Diagram   - generate the .drawio from the current analysis or any CSV.
  4. DRD       - versions, DRD import issues, rebuild from a DRD HTML, saved
                 part # rules.

All logic lives in session.py; this file only draws widgets.
"""
from __future__ import annotations

import os
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .session import Session, rebuild_drd, HHS_MODES, HHS_MODE_LABELS, APP_VERSION
from . import review as rv
from . import user_devices as udb

APP_TITLE = "Wire Diagram Builder"
POWER_CHOICES = {"STAR": ["C13"], "VANTAGE": ["L14-20", "L21-20", "C19"]}
CATEGORIES = ["off_deck", "on_deck", "pc", "hub"]


def _err(parent, title, exc):
    traceback.print_exc()
    messagebox.showerror(title, str(exc) or exc.__class__.__name__, parent=parent)


def _float_or_none(s):
    s = (s or "").strip()
    if not s:
        return None
    return float(s)


def _scrolled_tree(parent, columns, widths, height=14, selectmode="extended"):
    frm = ttk.Frame(parent)
    tree = ttk.Treeview(frm, columns=columns, show="headings", height=height,
                        selectmode=selectmode)
    for c, w in zip(columns, widths):
        tree.heading(c, text=c)
        tree.column(c, width=w, anchor="w", stretch=(w >= 150))
    sb = ttk.Scrollbar(frm, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return frm, tree


def _scrolled_text(parent, height=10):
    frm = ttk.Frame(parent)
    txt = tk.Text(frm, height=height, wrap="word", font=("Consolas", 9))
    sb = ttk.Scrollbar(frm, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return frm, txt


def _set_text(txt, s):
    txt.configure(state="normal")
    txt.delete("1.0", "end")
    txt.insert("1.0", s)
    txt.configure(state="disabled")


# ==========================================================================
# Dialogs
# ==========================================================================
class _Dialog(tk.Toplevel):
    def __init__(self, master, title):
        super().__init__(master)
        self.title(title)
        self.transient(master.winfo_toplevel())
        self.result = None
        self.body = ttk.Frame(self, padding=10)
        self.body.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda e: self.destroy())

    def buttons(self, ok_text="OK"):
        b = ttk.Frame(self, padding=(10, 0, 10, 10))
        b.pack(fill="x")
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(b, text=ok_text, command=self._ok).pack(side="right", padx=4)

    def run(self):
        self.grab_set()
        self.wait_window()
        return self.result

    def _ok(self):
        raise NotImplementedError


class DevicePickerDialog(_Dialog):
    """Search the DRD by name and pick one or more devices (no guessing)."""

    def __init__(self, master, names, title="Pick DRD device(s)", prompt="", multi=True,
                 show_qty=False, show_remember=False):
        super().__init__(master, title)
        self.names = list(names)
        if prompt:
            ttk.Label(self.body, text=prompt, wraplength=460).pack(anchor="w", pady=(0, 6))
        self.q = tk.StringVar()
        e = ttk.Entry(self.body, textvariable=self.q, width=50)
        e.pack(fill="x")
        e.focus_set()
        self.q.trace_add("write", lambda *a: self._filter())
        self.lb = tk.Listbox(self.body, height=16, width=60,
                             selectmode="extended" if multi else "browse", exportselection=False)
        self.lb.pack(fill="both", expand=True, pady=6)
        self.lb.bind("<Double-1>", lambda e: self._ok())
        opt = ttk.Frame(self.body)
        opt.pack(fill="x")
        self.qty = tk.StringVar(value="1")
        self.remember = tk.BooleanVar(value=False)
        if show_qty:
            ttk.Label(opt, text="Qty:").pack(side="left")
            ttk.Spinbox(opt, from_=1, to=99, textvariable=self.qty, width=5).pack(side="left", padx=4)
        if show_remember:
            ttk.Checkbutton(opt, text="Remember for this part #",
                            variable=self.remember).pack(side="left", padx=8)
        self._filter()
        self.buttons()

    def _filter(self):
        q = self.q.get().lower().split()
        self.lb.delete(0, "end")
        for n in self.names:
            if all(t in n.lower() for t in q):
                self.lb.insert("end", n)

    def _ok(self):
        sel = [self.lb.get(i) for i in self.lb.curselection()]
        if not sel:
            messagebox.showinfo("Pick a device", "Select at least one device.", parent=self)
            return
        try:
            qty = max(1, int(self.qty.get()))
        except ValueError:
            qty = 1
        self.result = {"names": sel, "qty": qty, "remember": self.remember.get()}
        self.destroy()


class CustomDeviceDialog(_Dialog):
    """A device that is not in the DRD (CPR custom item). Saved outside the DRD."""

    def __init__(self, master, name="", show_remember=False):
        super().__init__(master, "Custom device (not in DRD)")
        saved = udb.load_user_devices()
        self.saved = saved
        r = 0
        if saved:
            ttk.Label(self.body, text="Load saved:").grid(row=r, column=0, sticky="w")
            self.pick = ttk.Combobox(self.body, values=sorted(saved), state="readonly", width=32)
            self.pick.grid(row=r, column=1, sticky="w", pady=2)
            self.pick.bind("<<ComboboxSelected>>", self._load_saved)
            r += 1
        self.v = {k: tk.StringVar() for k in
                  ("name", "amperage", "watts", "power_cable", "comm_cable", "width", "height", "qty")}
        self.v["name"].set(name)
        self.v["power_cable"].set("C13 to NEMA 5-15")
        self.v["qty"].set("1")
        self.cat = tk.StringVar(value="off_deck")
        self.role = tk.StringVar(value="powered")
        rows = [("Name", "name"), ("Amperage (A)", "amperage"), ("or Watts @120V", "watts"),
                ("Power cable(s)", "power_cable"), ("Comm cable(s)", "comm_cable"),
                ("Block width px", "width"), ("Block height px", "height"), ("Qty", "qty")]
        for label, k in rows:
            ttk.Label(self.body, text=label + ":").grid(row=r, column=0, sticky="w")
            ttk.Entry(self.body, textvariable=self.v[k], width=34).grid(row=r, column=1,
                                                                       sticky="w", pady=2)
            r += 1
        ttk.Label(self.body, text="Category:").grid(row=r, column=0, sticky="w")
        ttk.Combobox(self.body, textvariable=self.cat, values=CATEGORIES, state="readonly",
                     width=31).grid(row=r, column=1, sticky="w", pady=2)
        r += 1
        ttk.Label(self.body, text="Role:").grid(row=r, column=0, sticky="w")
        ttk.Combobox(self.body, textvariable=self.role, values=["powered", "child"],
                     state="readonly", width=31).grid(row=r, column=1, sticky="w", pady=2)
        r += 1
        ttk.Label(self.body, text="Separate several cables with ' / '.  Blank size = 120x60.",
                  foreground="gray").grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1
        self.save = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.body, text="Save this device for reuse (outside the DRD)",
                        variable=self.save).grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1
        self.remember = tk.BooleanVar(value=False)
        if show_remember:
            ttk.Checkbutton(self.body, text="Remember for this part #",
                            variable=self.remember).grid(row=r, column=0, columnspan=2, sticky="w")
        self.buttons()

    def _load_saved(self, _e=None):
        d = self.saved.get(self.pick.get(), {})
        for k in ("name", "power_cable", "comm_cable"):
            self.v[k].set(d.get(k) or "")
        self.v["amperage"].set("" if d.get("amperage") is None else str(d["amperage"]))
        self.v["width"].set(str(d.get("width") or ""))
        self.v["height"].set(str(d.get("height") or ""))
        self.cat.set(d.get("category") or "off_deck")
        self.role.set(d.get("role") or "powered")

    def _ok(self):
        name = self.v["name"].get().strip()
        if not name:
            messagebox.showwarning("Name", "Enter a name.", parent=self)
            return
        try:
            amps = _float_or_none(self.v["amperage"].get())
            watts = _float_or_none(self.v["watts"].get())
            if amps is None and watts is not None:
                amps = round(watts / 120.0, 2)
            w = _float_or_none(self.v["width"].get())
            h = _float_or_none(self.v["height"].get())
            qty = max(1, int(self.v["qty"].get() or 1))
        except ValueError:
            messagebox.showwarning("Numbers", "Amperage, watts, size and qty must be numbers.",
                                   parent=self)
            return
        role = self.role.get()
        if role == "powered" and amps is None:
            messagebox.showwarning("Amperage", "A powered device needs an amperage or watts.",
                                   parent=self)
            return
        custom = {"name": name, "role": role, "category": self.cat.get(),
                  "amperage": amps if role == "powered" else None,
                  "amperage_status": "ok" if amps is not None else "none",
                  "amperage_raw": "" if amps is None else f"{amps:g}",
                  "power_cable": self.v["power_cable"].get().strip() if role == "powered" else "",
                  "comm_cable": self.v["comm_cable"].get().strip(),
                  "width": int(w) if w else None, "height": int(h) if h else None,
                  "part_numbers": [], "notes": "Custom device (not in DRD)"}
        if self.save.get():
            udb.save_user_device(name, custom["amperage"], custom["power_cable"],
                                 custom["comm_cable"], custom["category"],
                                 notes=custom["notes"])
            data = udb.load_user_devices()      # keep size/role too
            data[name].update({"role": role, "width": custom["width"], "height": custom["height"]})
            try:
                import json
                with open(udb._store_path(), "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except OSError:
                pass
        self.result = {"custom": custom, "qty": qty, "remember": self.remember.get()}
        self.destroy()


class EditDeviceDialog(_Dialog):
    def __init__(self, master, device, drd_amps_text):
        super().__init__(master, f"Edit - {device.name}")
        self.qty = tk.StringVar(value=str(device.qty))
        self.amps = tk.StringVar(value="" if device.amp_override is None else f"{device.amp_override:g}")
        ttk.Label(self.body, text=f"DRD / current amperage: {drd_amps_text}").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(self.body, text="Qty:").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(self.body, from_=1, to=99, textvariable=self.qty, width=8).grid(
            row=1, column=1, sticky="w")
        ttk.Label(self.body, text="Amperage override (A):").grid(row=2, column=0, sticky="w")
        ttk.Entry(self.body, textvariable=self.amps, width=10).grid(row=2, column=1, sticky="w")
        ttk.Label(self.body, text="Leave blank to use the DRD value. Use this for 'Varies', a "
                                  "CPR value, or the largest draw between swappable devices.",
                  wraplength=360, foreground="gray").grid(row=3, column=0, columnspan=2,
                                                          sticky="w", pady=4)
        self.buttons()

    def _ok(self):
        try:
            qty = max(1, int(self.qty.get()))
            amps = _float_or_none(self.amps.get())
        except ValueError:
            messagebox.showwarning("Numbers", "Qty and amperage must be numbers.", parent=self)
            return
        self.result = {"qty": qty, "amps": amps}
        self.destroy()


class PipettorDialog(_Dialog):
    def __init__(self, master, models, variants):
        super().__init__(master, "Add pipettor")
        self.variants = variants
        self.model = tk.StringVar(value=models[0] if models else "")
        self.variant = tk.StringVar()
        self.power = tk.StringVar()
        self.amps = tk.StringVar()
        ttk.Label(self.body, text="Model:").grid(row=0, column=0, sticky="w")
        cb = ttk.Combobox(self.body, textvariable=self.model, values=models, state="readonly",
                          width=24)
        cb.grid(row=0, column=1, sticky="w", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self._sync())
        ttk.Label(self.body, text="Variant:").grid(row=1, column=0, sticky="w")
        self.vcb = ttk.Combobox(self.body, textvariable=self.variant, state="readonly", width=24)
        self.vcb.grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(self.body, text="Power:").grid(row=2, column=0, sticky="w")
        self.pcb = ttk.Combobox(self.body, textvariable=self.power, state="readonly", width=24)
        self.pcb.grid(row=2, column=1, sticky="w", pady=2)
        self.pcb.bind("<<ComboboxSelected>>", lambda e: self._sync_amps())
        ttk.Label(self.body, text="Amperage (A):").grid(row=3, column=0, sticky="w")
        ttk.Entry(self.body, textvariable=self.amps, width=10).grid(row=3, column=1, sticky="w")
        ttk.Label(self.body, text="L14-20 / L21-20 VANTAGE gets its own dedicated circuit "
                                  "(no amperage). C19 single-phase = 10A.",
                  wraplength=320, foreground="gray").grid(row=4, column=0, columnspan=2,
                                                          sticky="w", pady=4)
        self._sync()
        self.buttons("Add")

    def _type(self):
        return "VANTAGE" if "vantage" in self.model.get().lower() else "STAR"

    def _sync(self):
        names = self.variants(self.model.get())
        self.vcb.configure(values=names)
        self.variant.set(names[0] if names else "")
        vals = POWER_CHOICES[self._type()]
        self.pcb.configure(values=vals)
        self.power.set(vals[0])
        self._sync_amps()

    def _sync_amps(self):
        t, p = self._type(), self.power.get()
        self.amps.set("6.3" if t == "STAR" else ("10" if p == "C19" else ""))

    def _ok(self):
        try:
            amps = _float_or_none(self.amps.get())
        except ValueError:
            messagebox.showwarning("Amperage", "Amperage must be a number.", parent=self)
            return
        self.result = {"model": self.model.get(), "variant": self.variant.get(),
                       "power": self.power.get(), "amps": amps}
        self.destroy()


class QtyResolveDialog(_Dialog):
    def __init__(self, master, conflict):
        super().__init__(master, "Resolve quantity")
        self.conflict = conflict
        ttk.Label(self.body, text=f"{conflict.part_number} {conflict.description}\n"
                                  "The Sales Order and the CPR disagree. Pick the final quantity.",
                  wraplength=460).pack(anchor="w", pady=(0, 8))
        self.choice = tk.StringVar(value="")
        grp = ttk.Frame(self.body)
        grp.pack(anchor="w")
        ttk.Radiobutton(grp, text=f"Sales Order: {conflict.so_qty}", value="so",
                        variable=self.choice).pack(anchor="w")
        ttk.Radiobutton(grp, text=f"CPR: {conflict.cpr_qty}", value="cpr",
                        variable=self.choice).pack(anchor="w")
        row = ttk.Frame(grp)
        row.pack(anchor="w")
        ttk.Radiobutton(row, text="Other:", value="other",
                        variable=self.choice).pack(side="left")
        self.other = tk.StringVar(value=str(conflict.final) if conflict.final is not None else "1")
        ttk.Spinbox(row, from_=1, to=999, width=6, textvariable=self.other).pack(side="left", padx=4)
        if conflict.final == conflict.so_qty:
            self.choice.set("so")
        elif conflict.final == conflict.cpr_qty:
            self.choice.set("cpr")
        elif conflict.final is not None:
            self.choice.set("other")
        self.buttons()

    def _ok(self):
        c = self.choice.get()
        if not c:
            messagebox.showinfo("Pick one option", "Pick one option.", parent=self)
            return
        if c == "so":
            qty = self.conflict.so_qty
        elif c == "cpr":
            qty = self.conflict.cpr_qty
        else:
            try:
                qty = int(self.other.get())
            except ValueError:
                messagebox.showwarning("Quantity", "Enter a whole number.", parent=self)
                return
            if qty < 1:
                messagebox.showwarning("Quantity", "Quantity must be at least 1.", parent=self)
                return
        self.result = qty
        self.destroy()


# ==========================================================================
# Tab 1 - Review
# ==========================================================================
class ReviewTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=6)
        self.app = app
        self._build()

    @property
    def s(self) -> Session:
        return self.app.session

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Sales Order / Quote (.xlsx):").pack(side="left")
        self.path = tk.StringVar()
        ttk.Entry(top, textvariable=self.path, width=60).pack(side="left", padx=4)
        ttk.Button(top, text="Browse...", command=self._browse).pack(side="left")
        ttk.Button(top, text="Load order", command=self._load).pack(side="left", padx=4)
        ttk.Button(top, text="Start without order", command=self._blank).pack(side="left")

        top2 = ttk.Frame(self)
        top2.pack(fill="x", pady=(4, 0))
        ttk.Label(top2, text="CPR (.docx, optional):").pack(side="left")
        self.cpr_path = tk.StringVar()
        ttk.Entry(top2, textvariable=self.cpr_path, width=60).pack(side="left", padx=4)
        ttk.Button(top2, text="Browse...", command=self._browse_cpr).pack(side="left")
        ttk.Button(top2, text="Run CPR check", command=self._run_cpr).pack(side="left", padx=4)
        ttk.Button(top2, text="Resolve qty...", command=self._resolve_qty).pack(side="left", padx=4)
        ttk.Button(top2, text="Clear CPR check", command=self._clear_cpr).pack(side="left", padx=4)

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, pady=6)

        # ---- left: relevant
        left = ttk.LabelFrame(pw, text="Relevant for diagram", padding=4)
        f, self.rel = _scrolled_tree(left, ("Device", "Qty", "Amps", "Source", "Order part #"),
                                     (230, 40, 110, 120, 140))
        f.pack(fill="both", expand=True)
        self.rel.tag_configure("need", background="#fff2cc")
        self.rel.tag_configure("paired", foreground="#6a1b9a")
        self.rel.tag_configure("cpr", background="#f8d7da")
        self.rel.bind("<Double-1>", lambda e: self._edit())
        lb = ttk.Frame(left)
        lb.pack(fill="x", pady=(4, 0))
        self.remember_out = tk.BooleanVar(value=False)
        ttk.Button(lb, text="Move to Not part  >", command=self._move_out).pack(side="left")
        ttk.Checkbutton(lb, text="Remember for this part #",
                        variable=self.remember_out).pack(side="left", padx=4)
        ttk.Button(lb, text="Edit qty / amps", command=self._edit).pack(side="left", padx=4)
        lb2 = ttk.Frame(left)
        lb2.pack(fill="x", pady=(4, 0))
        ttk.Button(lb2, text="Add customer-supplied (DRD)...", command=self._add_customer).pack(side="left")
        ttk.Button(lb2, text="Add custom device...", command=self._add_custom).pack(side="left", padx=4)
        ttk.Button(lb2, text="Apply DRD §7 pairing", command=self._pairing).pack(side="left", padx=4)
        pw.add(left, weight=3)

        # ---- right: excluded
        right = ttk.LabelFrame(pw, text="Not part of diagram", padding=4)
        f, self.exc = _scrolled_tree(right, ("Part #", "Description", "Qty", "Reason"),
                                     (110, 230, 40, 150))
        f.pack(fill="both", expand=True)
        self.exc.tag_configure("dev", foreground="#1565c0")
        self.exc.tag_configure("cpr", foreground="#c62828")
        rb = ttk.Frame(right)
        rb.pack(fill="x", pady=(4, 0))
        ttk.Button(rb, text="<  Map to DRD device...", command=self._map_drd).pack(side="left")
        ttk.Button(rb, text="<  Map as custom...", command=self._map_custom).pack(side="left", padx=4)
        ttk.Button(rb, text="<  Restore device", command=self._restore).pack(side="left")
        rb2 = ttk.Frame(right)
        rb2.pack(fill="x", pady=(4, 0))
        ttk.Button(rb2, text="Always ignore this part #", command=self._ignore).pack(side="left")
        pw.add(right, weight=2)

        cprf = self.cprf = ttk.LabelFrame(self, text="SO vs CPR check (optional)", padding=4)
        cprf.pack(fill="x", pady=(0, 6))
        f, self.cprt = _scrolled_tree(cprf, ("Status", "Type", "CPR item", "CPR qty", "SO item",
                                             "SO qty", "Note"), (100, 70, 230, 60, 200, 60, 300),
                                      height=6, selectmode="browse")
        f.pack(fill="both", expand=True)
        self.cprt.tag_configure("missing", background="#f8d7da")
        self.cprt.tag_configure("qty", background="#ffe0b2")
        self.cprt.tag_configure("qtyok", background="#e2f0d9")
        self.cprt.tag_configure("info", foreground="gray")
        self.cprt.bind("<Double-1>", lambda e: self._resolve_qty())

        # ---- bottom: pipettors + log
        bot = ttk.Frame(self)
        bot.pack(fill="x")
        pf = ttk.LabelFrame(bot, text="Pipettors (manual selection)", padding=4)
        pf.pack(side="left", fill="y")
        self.pip = tk.Listbox(pf, height=4, width=40, exportselection=False)
        self.pip.pack(side="left", fill="y")
        pb = ttk.Frame(pf)
        pb.pack(side="left", padx=4)
        ttk.Button(pb, text="Add...", command=self._add_pip).pack(fill="x")
        ttk.Button(pb, text="Remove", command=self._rm_pip).pack(fill="x", pady=2)
        lf = ttk.LabelFrame(bot, text="Messages", padding=4)
        lf.pack(side="left", fill="both", expand=True, padx=(6, 0))
        f, self.log = _scrolled_text(lf, height=5)
        f.pack(fill="both", expand=True)
        ttk.Label(self, text="Yellow rows need an amperage (double-click to edit). Purple rows "
                             "were added by DRD §7 pairing. Only part numbers are matched "
                             "automatically. Red rows came from the CPR and are not on the SO "
                             "(add them to the SO). Orange rows in the CPR check need a final "
                             "quantity.", foreground="gray").pack(anchor="w")

    # ---- helpers
    def say(self, *lines):
        self.log.configure(state="normal")
        for ln in lines:
            self.log.insert("end", ln + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh(self):
        st = self.s.state
        self.rel.delete(*self.rel.get_children())
        for d in st.relevant:
            tags = []
            if self.s.needs_amps(d):
                tags.append("need")
            if d.source == rv.SRC_CPR:
                tags.append("cpr")
            if d.source == rv.SRC_PAIRING:
                tags.append("paired")
            name = d.name + ("  [custom]" if d.is_custom else "")
            self.rel.insert("", "end", iid=d.key, tags=tags,
                            values=(name, d.qty, self.s.amps_text(d), d.source, ", ".join(d.lines)))
        self.exc.delete(*self.exc.get_children())
        for e in st.excluded:
            if e.device is not None:
                vals = (", ".join(e.device.lines), e.device.name, e.device.qty, e.reason)
                tags = ["dev"]
                if (e.reason or "").startswith("From CPR"):
                    tags.append("cpr")
                self.exc.insert("", "end", iid=e.key, tags=tuple(tags), values=vals)
            else:
                ln = e.line
                tags = ("cpr",) if (e.reason or "").startswith("From CPR") else ()
                self.exc.insert("", "end", iid=e.key, tags=tags,
                                values=(ln.part_number, ln.description, ln.qty, e.reason))
        self.pip.delete(0, "end")
        for p in st.pipettors:
            self.pip.insert("end", f"{p.model} - {p.label()}")
        self.cprt.delete(*self.cprt.get_children())
        chk = self.s.cpr
        title = "SO vs CPR check (optional)"
        if chk is not None:
            title += (f"  -  Order: {os.path.basename(self.s.order_path) or '(none)'}"
                      f"  |  CPR: {os.path.basename(chk.cpr_path)}")
        self.cprf.configure(text=title)
        if chk is not None:
            for r in chk.rows:
                cpr_item = " ".join(x for x in (r.cpr_sk_pn, r.cpr_part_name) if x)
                so_item = " ".join(x for x in (r.so_product_number, r.so_product_name) if x)
                cpr_qty = f"{r.cpr_qty:g}" if r.cpr_qty is not None else ""
                so_qty = f"{r.so_qty:g}" if r.so_qty is not None else ""
                vals = (r.status, r.type, cpr_item, cpr_qty, so_item, so_qty, r.note)
                conflict = None
                if r.status == "Qty mismatch" and r.type == "Device" and r.cpr_sk_pn:
                    conflict = next((c for c in chk.conflicts if c.part_number == r.cpr_sk_pn), None)
                if conflict is not None and conflict.resolved:
                    vals = (f"Resolved: {conflict.final}", r.type, cpr_item, cpr_qty,
                            so_item, so_qty, r.note)
                    self.cprt.insert("", "end", iid=conflict.key, tags=("qtyok",), values=vals)
                elif conflict is not None:
                    self.cprt.insert("", "end", iid=conflict.key, tags=("qty",), values=vals)
                elif r.status == "Qty mismatch":
                    self.cprt.insert("", "end", tags=("qty",), values=vals)
                elif r.status == "Only in CPR":
                    self.cprt.insert("", "end", tags=("missing",), values=vals)
                elif r.status in ("Info", "Out of scope"):
                    self.cprt.insert("", "end", tags=("info",), values=vals)
                else:
                    self.cprt.insert("", "end", values=vals)
        self.app.mark_stale()

    def _sel(self, tree):
        return list(tree.selection())

    # ---- actions
    def _browse(self):
        p = filedialog.askopenfilename(title="Sales Order / Quote",
                                       filetypes=[("Excel", "*.xlsx *.xlsm"), ("All", "*.*")])
        if p:
            self.path.set(p)

    def _load(self):
        p = self.path.get().strip()
        if not p or not os.path.exists(p):
            messagebox.showwarning("Order", "Choose an .xlsx file first.", parent=self)
            return
        try:
            st = self.s.load_order(p)
        except Exception as ex:
            _err(self, "Could not read the order", ex)
            return
        self.app.analysis.project.set(self.s.options.project_name)
        self.refresh()
        self.say(f"Loaded {os.path.basename(p)}: {len(st.relevant)} DRD device(s), "
                 f"{len(st.excluded)} line(s) not part of the diagram.")
        if not st.pipettors:
            self.say("Add the pipettor(s) with 'Add...' in the Pipettors box.")

    def _blank(self):
        self.s.new_blank()
        self.refresh()
        self.say("Empty device list. Add devices and pipettors by hand.")

    def _move_out(self):
        keys = self._sel(self.rel)
        if keys:
            self.s.move_out(keys, self.remember_out.get())
            self.refresh()

    def _edit(self):
        keys = self._sel(self.rel)
        if len(keys) != 1:
            return
        d = self.s.state.find(keys[0])
        r = EditDeviceDialog(self, d, self.s.amps_text(d)).run()
        if r:
            self.s.edit_device(d.key, r["qty"], r["amps"])
            self.refresh()

    def _add_customer(self):
        r = DevicePickerDialog(self, self.s.drd.all_names(), "Add customer-supplied device",
                               "Pick the DRD device(s) the customer supplies.", show_qty=True).run()
        if r:
            for n in r["names"]:
                try:
                    self.s.add_customer(n, r["qty"])
                except ValueError as ex:
                    _err(self, "Not added", ex)
            self.refresh()

    def _add_custom(self):
        r = CustomDeviceDialog(self).run()
        if r:
            self.s.add_custom(r["custom"], r["qty"])
            self.refresh()

    def _pairing(self):
        msgs, warns = self.s.apply_pairing()
        self.refresh()
        self.say(*(msgs or ["DRD §7 pairing: nothing to add."]))
        if warns:
            self.say(*["Warning: " + w for w in warns])

    def _map_drd(self):
        keys = self._sel(self.exc)
        if len(keys) != 1:
            messagebox.showinfo("Map", "Select one line.", parent=self)
            return
        e = self.s.state.find_excluded(keys[0])
        desc = e.line.description if e.line else e.device.name
        r = DevicePickerDialog(self, self.s.drd.all_names(), "Map line to DRD device(s)",
                               f"'{desc}' becomes the selected device(s). Pick several for a kit.",
                               show_remember=bool(e.line and e.line.part_number)).run()
        if r:
            try:
                self.s.map_line(e.key, r["names"], None, r["remember"])
            except Exception as ex:
                _err(self, "Not mapped", ex)
            self.refresh()

    def _map_custom(self):
        keys = self._sel(self.exc)
        if len(keys) != 1:
            messagebox.showinfo("Map", "Select one line.", parent=self)
            return
        e = self.s.state.find_excluded(keys[0])
        desc = e.line.description if e.line else e.device.name
        r = CustomDeviceDialog(self, desc, show_remember=bool(e.line and e.line.part_number)).run()
        if r:
            self.s.map_line(e.key, (), r["custom"], r["remember"])
            self.refresh()

    def _restore(self):
        keys = [k for k in self._sel(self.exc) if self.s.state.find_excluded(k).device is not None]
        if not keys:
            messagebox.showinfo("Restore", "Select a device you moved out (blue rows). Order "
                                           "lines need 'Map to DRD device'.", parent=self)
            return
        self.s.restore(keys)
        self.refresh()

    def _ignore(self):
        keys = self._sel(self.exc)
        self.s.ignore_always(keys)
        self.say(f"Saved 'always ignore' for {len(keys)} part #(s).")
        self.app.drd_tab.refresh()

    def _add_pip(self):
        r = PipettorDialog(self, self.s.pipettor_models(), self.s.pipettor_variants).run()
        if r:
            self.s.add_pipettor(r["model"], r["power"], r["amps"], r["variant"])
            self.refresh()

    def _rm_pip(self):
        sel = self.pip.curselection()
        if sel:
            self.s.remove_pipettor(sel[0])
            self.refresh()

    def _browse_cpr(self):
        p = filedialog.askopenfilename(title="CPR",
                                       filetypes=[("Word", "*.docx"), ("All", "*.*")])
        if p:
            self.cpr_path.set(p)

    def _run_cpr(self):
        p = self.cpr_path.get().strip()
        if not p or not os.path.exists(p):
            messagebox.showwarning("CPR", "Choose a CPR .docx file first.", parent=self)
            return
        cpr_dir = os.path.basename(os.path.dirname(os.path.abspath(p)))
        if not self.s.order_path or self.s.cpr_folder_mismatch(p):
            so = self.s.find_order_for_cpr(p)
            if self.s.order_path:
                head = (f"The loaded order is from a different folder than the CPR.\n\n"
                        f"Order: {os.path.basename(os.path.dirname(self.s.order_path))}/"
                        f"{os.path.basename(self.s.order_path)}\n"
                        f"CPR: {cpr_dir}/{os.path.basename(p)}\n\n")
            else:
                head = "No order is loaded.\n\n"
            if so:
                ans = messagebox.askyesnocancel(
                    "Order / CPR mismatch",
                    head + f"Load {os.path.basename(so)} from the CPR's folder first?\n\n"
                    "Yes = load it, No = keep the current order, Cancel = stop.", parent=self)
                if ans is None:
                    return
                if ans:
                    self.path.set(so)
                    self._load()
                    if self.s.order_path != so:
                        return
            elif not messagebox.askokcancel("Order / CPR mismatch",
                                            head + "No Sales Order Confirmation or Dynamics order-lines "
                                            "export was found in the CPR's folder. Run the check anyway?", parent=self):
                return
        try:
            msgs = self.s.run_cpr_check(p)
        except Exception as ex:
            _err(self, "Could not run the CPR check", ex)
            return
        self.refresh()
        self.say(f"Order: {os.path.basename(self.s.order_path) or '(none)'} | "
                 f"CPR: {os.path.basename(p)}", *msgs)

    def _resolve_qty(self):
        chk = self.s.cpr
        if chk is None:
            return
        c = None
        sel = self.cprt.selection()
        if sel:
            c = chk.find_conflict(sel[0])
        if c is None and chk.unresolved():
            c = chk.unresolved()[0]
        if c is None:
            messagebox.showinfo("No quantity conflicts", "No quantity conflicts to resolve.",
                                parent=self)
            return
        r = QtyResolveDialog(self, c).run()
        if r is not None:
            self.s.resolve_qty(c.key, r)
            self.refresh()
            self.say(f"{c.part_number}: final quantity {r}.")

    def _clear_cpr(self):
        self.s.clear_cpr_check()
        self.refresh()
        self.say("CPR check cleared.")


# ==========================================================================
# Tab 2 - Analysis
# ==========================================================================
class AnalysisTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=6)
        self.app = app
        self._build()

    @property
    def s(self) -> Session:
        return self.app.session

    def _build(self):
        opt = ttk.LabelFrame(self, text="Options", padding=6)
        opt.pack(fill="x")
        self.project = tk.StringVar()
        self.fac = tk.StringVar(value="15A")
        self.main_pc = tk.StringVar(value="(from order)")
        self.hhs = tk.StringVar(value=HHS_MODE_LABELS["auto"])
        self.iso = tk.BooleanVar(value=True)
        self.over = tk.BooleanVar(value=False)
        ttk.Label(opt, text="Project:").grid(row=0, column=0, sticky="w")
        ttk.Entry(opt, textvariable=self.project, width=30).grid(row=0, column=1, sticky="w")
        ttk.Label(opt, text="Facility circuits:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        fr = ttk.Frame(opt)
        fr.grid(row=0, column=3, sticky="w")
        for v in ("15A", "20A"):
            ttk.Radiobutton(fr, text=v, value=v, variable=self.fac).pack(side="left")
        ttk.Label(opt, text="Main PC:").grid(row=1, column=0, sticky="w")
        self.pc_cb = ttk.Combobox(opt, textvariable=self.main_pc, state="readonly", width=28)
        self.pc_cb.grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(opt, text="HHS connection:").grid(row=1, column=2, sticky="w", padx=(12, 0))
        ttk.Combobox(opt, textvariable=self.hhs, state="readonly", width=30,
                     values=[HHS_MODE_LABELS[m] for m in HHS_MODES]).grid(row=1, column=3, sticky="w")
        ttk.Checkbutton(opt, text="Keep a C19 VANTAGE on its own circuit",
                        variable=self.iso).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(opt, text="Approve going over 80% (up to 100% of rating)",
                        variable=self.over).grid(row=2, column=2, columnspan=2, sticky="w")
        bf = ttk.Frame(opt)
        bf.grid(row=0, column=4, rowspan=3, sticky="ne", padx=(16, 0))
        ttk.Button(bf, text="Run analysis", command=self.run).pack(fill="x")
        self.btn_csv = ttk.Button(bf, text="Export CSV...", command=self._export, state="disabled")
        self.btn_csv.pack(fill="x", pady=2)
        ttk.Button(bf, text="Save report (.txt)...", command=self._report).pack(fill="x")

        mid = ttk.PanedWindow(self, orient="horizontal")
        mid.pack(fill="both", expand=True, pady=6)
        cf = ttk.LabelFrame(mid, text="Circuits", padding=4)
        frm = ttk.Frame(cf)
        frm.pack(fill="both", expand=True)
        self.ct = ttk.Treeview(frm, columns=("Amps", "Plugs", "Notes"), height=16)
        self.ct.heading("#0", text="Circuit / load")
        self.ct.column("#0", width=260)
        for c, w in (("Amps", 70), ("Plugs", 50), ("Notes", 160)):
            self.ct.heading(c, text=c)
            self.ct.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(frm, orient="vertical", command=self.ct.yview)
        self.ct.configure(yscrollcommand=sb.set)
        self.ct.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.ct.tag_configure("over", foreground="#c62828")
        self.ct.tag_configure("pinned", foreground="#1565c0")
        pin = ttk.Frame(cf)
        pin.pack(fill="x", pady=(4, 0))
        ttk.Label(pin, text="Move selected load(s) to:").pack(side="left")
        self.pin_n = tk.StringVar()
        self.pin_cb = ttk.Combobox(pin, textvariable=self.pin_n, state="readonly", width=12)
        self.pin_cb.pack(side="left", padx=4)
        ttk.Button(pin, text="Move", command=self._pin).pack(side="left")
        ttk.Button(pin, text="Swap 2 loads", command=self._swap).pack(side="left", padx=4)
        ttk.Button(pin, text="Clear moves", command=self._clear).pack(side="left")
        mid.add(cf, weight=2)

        rf = ttk.Frame(mid)
        isf = ttk.LabelFrame(rf, text="Blocking issues (fix before export)", padding=4)
        isf.pack(fill="x")
        self.issues = tk.Listbox(isf, height=5, foreground="#c62828")
        self.issues.pack(fill="x")
        repf = ttk.LabelFrame(rf, text="Report", padding=4)
        repf.pack(fill="both", expand=True, pady=(6, 0))
        f, self.rep = _scrolled_text(repf, height=18)
        f.pack(fill="both", expand=True)
        mid.add(rf, weight=3)
        self.status = ttk.Label(self, text="Not run.", foreground="gray")
        self.status.pack(anchor="w")

    def refresh_choices(self):
        self.pc_cb.configure(values=["(from order)"] + list(self.s.drd.pcs.keys()))

    def mark_stale(self):
        if self.s.result is None:
            self.btn_csv.configure(state="disabled")
            self.status.configure(text="Device list changed - run the analysis again.",
                                  foreground="#b26a00")

    def _sync_options(self):
        o = self.s.options
        o.project_name = self.project.get().strip()
        o.facility_circuit = self.fac.get()
        o.main_pc = "" if self.main_pc.get() == "(from order)" else self.main_pc.get()
        inv = {v: k for k, v in HHS_MODE_LABELS.items()}
        o.hhs_mode = inv.get(self.hhs.get(), "auto")
        o.isolate_vantage = self.iso.get()
        o.over_80_approved = self.over.get()

    def run(self):
        if not self.s.state.pipettors and not messagebox.askyesno(
                "No pipettor", "No pipettor selected. Run anyway?", parent=self):
            return
        self._sync_options()
        try:
            self.s.run_analysis()
        except Exception as ex:
            _err(self, "Analysis failed", ex)
            return
        self.show()

    def show(self):
        res = self.s.result
        self.ct.delete(*self.ct.get_children())
        self.issues.delete(0, "end")
        if res is None:
            return
        pins = self.s.moved
        targets = [f"Circuit {c.num}" for c in res.circuits] + ["New circuit"]
        self.pin_cb.configure(values=targets)
        if self.pin_n.get() not in targets:
            self.pin_n.set(targets[0])
        for c in res.circuits:
            pct = (c.load / float(c.rating.rstrip("A")) * 100) if c.rating.rstrip("A") else 0
            note = c.outlet + (" | power strip" if c.power_strip else "") + \
                (" | dedicated" if c.dedicated else "")
            tag = ("over",) if c.load > c.cap + 1e-9 else ()
            cid = f"C{c.num}"
            self.ct.insert("", "end", iid=cid, open=True, tags=tag,
                           text=f"Circuit {c.num} ({c.rating}, {pct:.0f}%)",
                           values=(f"{c.load:.1f}", c.connections, note))
            for l in c.loads:
                self.ct.insert(cid, "end", iid="L:" + l.key,
                               tags=("pinned",) if l.key in pins else (),
                               text=l.key, values=(f"{l.amps:.1f}", l.plugs,
                                                   "moved by user" if l.key in pins else ""))
        for i in res.issues:
            self.issues.insert("end", i)
        _set_text(self.rep, res.report)
        self.btn_csv.configure(state="normal" if res.ok else "disabled")
        self.status.configure(
            text=("OK - ready to export." if res.ok else f"{len(res.issues)} blocking issue(s).")
            + f"  {len(res.warnings)} warning(s), {len(res.assumptions)} assumption(s)."
            + ("  Edited circuit layout (Clear moves to undo)." if self.s.options.manual_circuits else ""),
            foreground="#2e7d32" if res.ok else "#c62828")
        self.app.diagram.refresh_source()

    def _pin(self):
        sel = [i for i in self.ct.selection() if i.startswith("L:")]
        if not sel:
            messagebox.showinfo("Move", "Select a load (not a circuit row).", parent=self)
            return
        t = self.pin_n.get()
        if not t:
            return
        target = "new" if t == "New circuit" else int(t.split()[-1])
        self._sync_options()
        try:
            self.s.move_loads([i[2:] for i in sel], target)
        except Exception as ex:
            _err(self, "Analysis failed", ex)
        self.show()

    def _swap(self):
        sel = [i for i in self.ct.selection() if i.startswith("L:")]
        if len(sel) != 2:
            messagebox.showinfo("Swap", "Select exactly two loads (Ctrl+click).", parent=self)
            return
        self._sync_options()
        try:
            self.s.swap_loads(sel[0][2:], sel[1][2:])
        except Exception as ex:
            _err(self, "Analysis failed", ex)
        self.show()

    def _clear(self):
        self._sync_options()
        self.s.clear_pins()
        self.show()

    def _export(self):
        name = (self.s.options.project_name or "power_distribution").replace(" ", "_")
        p = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"{name}_power_distribution.csv",
                                         filetypes=[("CSV", "*.csv")])
        if not p:
            return
        try:
            self.s.export_csv(p)
            self.app.diagram.csv.set(p)
            messagebox.showinfo("Saved", f"Saved {p}", parent=self)
        except Exception as ex:
            _err(self, "Not exported", ex)

    def _report(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")])
        if p:
            try:
                self.s.export_report(p)
            except Exception as ex:
                _err(self, "Not saved", ex)


# ==========================================================================
# Tab 3 - Diagram
# ==========================================================================
class DiagramTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=6)
        self.app = app
        self.src = tk.StringVar(value="csv")
        self.csv = tk.StringVar()
        top = ttk.LabelFrame(self, text="Source", padding=6)
        top.pack(fill="x")
        self.rb_cur = ttk.Radiobutton(top, text="Current analysis", value="current",
                                      variable=self.src, command=self._summary)
        self.rb_cur.grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(top, text="Power Distribution CSV:", value="csv", variable=self.src,
                        command=self._summary).grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.csv, width=70).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Button(top, text="Browse...", command=self._browse).grid(row=1, column=2)
        ttk.Button(top, text="Generate .drawio...", command=self._gen).grid(row=0, column=3,
                                                                           rowspan=2, padx=12)
        f, self.txt = _scrolled_text(self, height=24)
        f.pack(fill="both", expand=True, pady=6)
        self.refresh_source()

    @property
    def s(self) -> Session:
        return self.app.session

    def refresh_source(self):
        ok = self.s.can_export()
        self.rb_cur.configure(state="normal" if ok else "disabled")
        if ok and self.src.get() == "csv" and not self.csv.get():
            self.src.set("current")
        if not ok and self.src.get() == "current":
            self.src.set("csv")
        self._summary()

    def _browse(self):
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if p:
            self.csv.set(p)
            self.src.set("csv")
            self._summary()

    def _model(self):
        if self.src.get() == "current":
            return self.s.result.model if self.s.can_export() else None
        p = self.csv.get().strip()
        if p and os.path.exists(p):
            return self.s.load_csv(p)
        return None

    def _summary(self):
        try:
            m = self._model()
        except Exception as ex:
            _set_text(self.txt, f"Could not read CSV: {ex}")
            return
        if m is None:
            _set_text(self.txt, "Pick a source.")
            return
        lines = [f"Project: {m.meta.get('project_name', '')}",
                 f"Schema {m.meta.get('schema_version', '')} | DRD v{m.meta.get('drd_version', '?')}"
                 f" | StyleGuide v{m.meta.get('styleguide_version', '?')}", "",
                 "Circuits:"]
        for c in m.circuits:
            lines.append(f"  {c.circuit}: {c.load_a}A {c.voltage_outlet}"
                         + (" (power strip)" if str(c.power_strip).lower() in ("yes", "true") else "")
                         + f" - {c.devices}")
        lines += ["", f"Devices ({len(m.devices)}):"]
        for d in m.devices:
            lines.append(f"  {d.id:<34} {d.category:<9} {d.role:<9} circuit {d.circuit or '-'}")
        lines += ["", f"Connections: {len(m.connections)}"]
        _set_text(self.txt, "\n".join(lines))

    def _gen(self):
        name = (self.s.options.project_name or "wiring_diagram").replace(" ", "_")
        p = filedialog.asksaveasfilename(defaultextension=".drawio",
                                         initialfile=f"{name}_Wiring_Diagram.drawio",
                                         filetypes=[("draw.io", "*.drawio")])
        if not p:
            return
        try:
            csv_path = self.csv.get().strip() if self.src.get() == "csv" else None
            if self.src.get() == "csv" and not csv_path:
                raise RuntimeError("Choose a CSV first.")
            self.s.generate_diagram(p, csv_path)
            messagebox.showinfo("Saved", f"Saved {p}\nOpen it in draw.io to review.", parent=self)
        except Exception as ex:
            _err(self, "Diagram not generated", ex)


# ==========================================================================
# Tab 4 - DRD
# ==========================================================================
class DRDTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=6)
        self.app = app
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.ver = ttk.Label(top, text="", font=("", 10, "bold"))
        self.ver.pack(side="left")
        ttk.Button(top, text="Rebuild from DRD HTML...", command=self._rebuild).pack(side="right")
        vf = ttk.LabelFrame(self, text="DRD import issues", padding=4)
        vf.pack(fill="both", expand=True, pady=6)
        f, self.val = _scrolled_text(vf, height=10)
        f.pack(fill="both", expand=True)
        rf = ttk.LabelFrame(self, text="Saved part # rules (stored outside the DRD)", padding=4)
        rf.pack(fill="both", expand=True)
        f, self.rules = _scrolled_tree(rf, ("Part #", "Action", "Devices", "Note"),
                                       (120, 70, 250, 250), height=8)
        f.pack(fill="both", expand=True)
        ttk.Button(rf, text="Delete selected rule(s)", command=self._del).pack(anchor="w", pady=(4, 0))
        self.refresh()

    @property
    def s(self) -> Session:
        return self.app.session

    def refresh(self):
        self.ver.configure(text=self.s.version_line() + f"  |  App v{APP_VERSION}")
        v = list(dict.fromkeys(self.s.drd.validation))
        _set_text(self.val, "\n".join("- " + x for x in v) if v else "No issues found.")
        self.rules.delete(*self.rules.get_children())
        for k, r in sorted(self.s.overrides.rules.items()):
            devs = ", ".join(r.get("devices", []) + [c.get("name", "custom") for c in r.get("custom", [])])
            self.rules.insert("", "end", iid=k, values=(r.get("pn", k), r.get("action"), devs,
                                                        r.get("note", "")))

    def _del(self):
        for k in self.rules.selection():
            pn = self.s.overrides.rules.get(k, {}).get("pn", k)
            self.s.overrides.remove(pn)
        self.refresh()

    def _rebuild(self):
        p = filedialog.askopenfilename(title="DeviceReferenceDatabase_v*.html",
                                       filetypes=[("HTML", "*.html *.htm")])
        if not p:
            return
        try:
            drd = rebuild_drd(p)
        except Exception as ex:
            _err(self, "DRD not rebuilt", ex)
            return
        self.app.set_drd(drd, os.path.basename(p))
        messagebox.showinfo("DRD", f"Loaded DRD v{drd.version}. Reload the order on the Review "
                                   f"tab so it is matched against the new DRD.", parent=self)


# ==========================================================================
class MainWindow(tk.Tk):
    def __init__(self, session: Session):
        super().__init__()
        self.session = session
        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.geometry("1280x820")
        self.minsize(1000, 640)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self.drd_tab = None
        self.diagram = None
        self.analysis = AnalysisTab(nb, self)
        self.review = ReviewTab(nb, self)
        self.diagram = DiagramTab(nb, self)
        self.drd_tab = DRDTab(nb, self)
        nb.add(self.review, text="1. Review order")
        nb.add(self.analysis, text="2. Power analysis")
        nb.add(self.diagram, text="3. Wiring diagram")
        nb.add(self.drd_tab, text="DRD")
        self.analysis.refresh_choices()
        self.review.refresh()
        self.review.say(self.session.version_line())

    def mark_stale(self):
        if self.analysis is not None:
            self.analysis.mark_stale()
        if self.diagram is not None:
            self.diagram.refresh_source()

    def set_drd(self, drd, source):
        self.session.drd = drd
        self.session.drd_source = source
        self.session.result = None
        self.analysis.refresh_choices()
        self.drd_tab.refresh()
        self.review.refresh()
        self.review.say(self.session.version_line())


def main():
    try:
        session = Session.open()
    except Exception as ex:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_TITLE, f"Could not load the DRD:\n{ex}")
        return
    MainWindow(session).mainloop()


if __name__ == "__main__":
    main()

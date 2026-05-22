"""Apple-Style Multi-Step Web-Formular fuer Kunden-Anfragen.

Single-File-HTML mit:
- 3 Schritte: Was -> Details -> Kontakt
- Inline JS fuer Step-Navigation (kein Build-Step)
- SF Pro System-Font Stack
- Schwarz-Anthrazit Pill-Buttons, viel Whitespace
- Apple-Easing (0.32, 0.72, 0, 1), animierte Card-Selects mit Checkmark
- Sticky Progress-Bar, Step-Counter im Mono-Stil
- Mobile-responsive

Auch in diesem Modul: gemeinsame Status-Seiten (Erfolg / Fehler / Token-ungueltig),
damit Form und Status-Seiten visuell durchgaengig wirken.

Genutzt von core/api/anfrage_routes.py.
"""
from __future__ import annotations

import html as _html


# ============================================================
# Field-Gruppierung in 3 Steps
# ============================================================

def split_fields_into_steps(fields: list[dict]) -> list[dict]:
    """Teilt Felder in 3 logische Steps.

    Heuristik:
    - Step 1 'Was?': produkt + beschreibung (oder erste 2 Felder)
    - Step 2 'Details': material/masse/aufstellort/termin/budget/lieferung
    - Step 3 'Kontakt': telefon + anmerkungen + Submit
    """
    step1_keys = {"produkt", "beschreibung", "anliegen"}
    step3_keys = {"telefon", "anmerkungen", "name", "email"}

    s1, s2, s3 = [], [], []
    for f in fields:
        n = f.get("name", "")
        if n in step1_keys:
            s1.append(f)
        elif n in step3_keys:
            s3.append(f)
        else:
            s2.append(f)

    # Falls eine Gruppe leer ist, ausgleichen
    if not s1 and fields:
        s1.append(fields[0])
        s2 = [f for f in s2 if f != fields[0]]
    if not s3 and len(fields) > 1:
        s3.append(fields[-1])
        s2 = [f for f in s2 if f != fields[-1]]

    return [
        {"title": "Was möchtest du anfertigen lassen?", "subtitle": "Erzähl uns kurz worum es geht.", "fields": s1},
        {"title": "Die Details.", "subtitle": "Damit wir dir ein passendes Angebot machen können.", "fields": s2},
        {"title": "Wie können wir dich erreichen?", "subtitle": "Wir melden uns bald mit einem Vorschlag.", "fields": s3},
    ]


# ============================================================
# Field-Renderer
# ============================================================

def render_field(field: dict) -> str:
    """Rendert ein einzelnes Feld als HTML."""
    name = _html.escape(field.get("name", ""))
    label = _html.escape(field.get("label", name))
    required = field.get("required", False)
    ftype = field.get("type", "text")
    placeholder = _html.escape(field.get("placeholder", ""))
    options = field.get("options", []) or []
    req_attr = "required" if required else ""
    req_mark = '<span class="req">*</span>' if required else ""

    label_html = f'<label class="field-label" for="f-{name}">{label}{req_mark}</label>'

    if ftype in ("text", "tel"):
        input_type = "tel" if ftype == "tel" else "text"
        return f'''
        <div class="field">
            {label_html}
            <input type="{input_type}" id="f-{name}" name="{name}" {req_attr}
                   placeholder="{placeholder}" class="field-input" autocomplete="off">
        </div>'''

    if ftype == "date":
        return f'''
        <div class="field">
            {label_html}
            <input type="date" id="f-{name}" name="{name}" {req_attr} class="field-input">
        </div>'''

    if ftype == "textarea":
        return f'''
        <div class="field">
            {label_html}
            <textarea id="f-{name}" name="{name}" {req_attr}
                      placeholder="{placeholder}" rows="4" class="field-textarea"></textarea>
        </div>'''

    if ftype == "radio":
        # Apple-Style: grosse Karten mit Checkmark-Indicator rechts
        opts_html = []
        for opt in options:
            opt_esc = _html.escape(opt)
            opt_id = _html.escape(f"{name}-{opt}".replace(" ", "_"))
            opts_html.append(f'''
                <label class="card-option" for="r-{opt_id}">
                    <input type="radio" id="r-{opt_id}" name="{name}" value="{opt_esc}"
                           {req_attr} class="card-radio">
                    <span class="card-label">{opt_esc}</span>
                    <span class="card-check" aria-hidden="true"></span>
                </label>''')
        return f'''
        <div class="field">
            {label_html}
            <div class="card-grid">
                {"".join(opts_html)}
            </div>
        </div>'''

    if ftype == "checkbox_multi":
        opts_html = []
        for opt in options:
            opt_esc = _html.escape(opt)
            opt_id = _html.escape(f"{name}-{opt}".replace(" ", "_"))
            opts_html.append(f'''
                <label class="card-option" for="c-{opt_id}">
                    <input type="checkbox" id="c-{opt_id}" name="{name}[]" value="{opt_esc}"
                           class="card-radio">
                    <span class="card-label">{opt_esc}</span>
                    <span class="card-check" aria-hidden="true"></span>
                </label>''')
        return f'''
        <div class="field">
            {label_html}
            <div class="card-grid">
                {"".join(opts_html)}
            </div>
        </div>'''

    if ftype == "select":
        opts_html = ['<option value="">— bitte wählen —</option>']
        for opt in options:
            opt_esc = _html.escape(opt)
            opts_html.append(f'<option value="{opt_esc}">{opt_esc}</option>')
        return f'''
        <div class="field">
            {label_html}
            <div class="select-wrap">
                <select id="f-{name}" name="{name}" {req_attr} class="field-input field-select">
                    {"".join(opts_html)}
                </select>
            </div>
        </div>'''

    if ftype == "masse":
        return f'''
        <div class="field">
            {label_html}
            <div class="masse-grid">
                <div>
                    <span class="masse-sub">Höhe (cm)</span>
                    <input type="number" name="masse_hoehe" placeholder="100" class="field-input" min="0">
                </div>
                <div>
                    <span class="masse-sub">Breite (cm)</span>
                    <input type="number" name="masse_breite" placeholder="100" class="field-input" min="0">
                </div>
                <div>
                    <span class="masse-sub">Tiefe (cm)</span>
                    <input type="number" name="masse_tiefe" placeholder="40" class="field-input" min="0">
                </div>
            </div>
        </div>'''

    if ftype == "file":
        return f'''
        <div class="field">
            {label_html}
            <input type="file" id="f-{name}" name="{name}[]" {req_attr}
                   accept="image/*,application/pdf" multiple
                   class="field-input field-file">
            <small class="field-hint">
                Bilder oder PDF, maximal 3 Dateien à 5 MB.
            </small>
        </div>'''

    # Fallback
    return f'''
    <div class="field">
        {label_html}
        <input type="text" id="f-{name}" name="{name}" {req_attr}
               placeholder="{placeholder}" class="field-input">
    </div>'''


# ============================================================
# Shared CSS – Apple-Style Design System
# ============================================================

# Apple's signature easing
_EASE = "cubic-bezier(0.32, 0.72, 0, 1)"

# Embedded SVG-Checkmark als data-URI fuer ::after auf gewaehlten Karten
_CHECK_SVG = (
    "url(\"data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
    "stroke='white' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'>"
    "<polyline points='20 6 9 17 4 12'/></svg>\")"
)

CSS = f"""
:root {{
    --color-bg: #f5f5f7;
    --color-canvas: #ffffff;
    --color-text: #1d1d1f;
    --color-text-muted: #6e6e73;
    --color-text-subtle: #86868b;
    --color-border: rgba(0, 0, 0, 0.08);
    --color-border-hover: rgba(0, 0, 0, 0.18);
    --color-accent: #1d1d1f;
    --color-accent-hover: #000000;
    --color-error: #d70015;
    --color-tint: rgba(0, 0, 0, 0.04);
    --radius: 14px;
    --radius-sm: 10px;
    --radius-pill: 980px;
    --shadow-card: 0 1px 2px rgba(0, 0, 0, 0.04), 0 8px 24px rgba(0, 0, 0, 0.04);
    --shadow-input-focus: 0 0 0 4px rgba(29, 29, 31, 0.08);
    --shadow-button: 0 1px 2px rgba(0, 0, 0, 0.04), 0 4px 14px rgba(0, 0, 0, 0.10);
    --shadow-button-hover: 0 2px 4px rgba(0, 0, 0, 0.06), 0 10px 24px rgba(0, 0, 0, 0.14);
    --ease: {_EASE};
    --transition: 260ms var(--ease);
}}

* {{ box-sizing: border-box; }}

html, body {{
    margin: 0;
    padding: 0;
    background: var(--color-bg);
    color: var(--color-text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
                 "Inter", "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 17px;
    line-height: 1.47;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    text-rendering: optimizeLegibility;
}}

body {{
    /* Sehr subtiler Highlight oben - kaum sichtbar, aber gibt Tiefe */
    background-image:
        radial-gradient(1200px 600px at 50% -200px, rgba(255,255,255,0.7) 0%, transparent 60%),
        linear-gradient(180deg, #fafafa 0%, #f5f5f7 400px);
    background-attachment: fixed;
    min-height: 100vh;
}}

/* Sticky Progress-Bar oben am Rand */
.progress-wrap {{
    position: sticky;
    top: 0;
    z-index: 10;
    background: rgba(245, 245, 247, 0.72);
    backdrop-filter: saturate(180%) blur(20px);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    padding: 12px 0 11px;
    border-bottom: 1px solid rgba(0, 0, 0, 0.04);
}}

.progress {{
    max-width: 600px;
    margin: 0 auto;
    height: 3px;
    background: rgba(0, 0, 0, 0.08);
    border-radius: 99px;
    overflow: hidden;
}}

.progress-bar {{
    height: 100%;
    width: 33%;
    background: var(--color-accent);
    border-radius: 99px;
    transition: width 600ms var(--ease);
}}

.page {{
    max-width: 600px;
    margin: 0 auto;
    padding: 56px 24px 96px;
}}

.brand {{
    font-size: 12px;
    font-weight: 500;
    color: var(--color-text-subtle);
    letter-spacing: 0.12em;
    text-transform: uppercase;
    margin-bottom: 56px;
}}

/* Steps (mit fade+slide animation) */
.step {{
    display: none;
    animation: stepIn 520ms var(--ease);
}}

.step.active {{ display: block; }}

@keyframes stepIn {{
    from {{ opacity: 0; transform: translateY(20px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
}}

.step-counter {{
    font-family: ui-monospace, "SF Mono", "Menlo", "Consolas", monospace;
    font-size: 12px;
    font-weight: 500;
    color: var(--color-text-subtle);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-bottom: 20px;
}}

h1 {{
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", sans-serif;
    font-size: 44px;
    font-weight: 600;
    letter-spacing: -0.025em;
    line-height: 1.08;
    margin: 0 0 14px;
    color: var(--color-text);
}}

.subtitle {{
    font-size: 19px;
    color: var(--color-text-muted);
    margin: 0 0 44px;
    line-height: 1.42;
    letter-spacing: -0.01em;
}}

/* Felder */
.field {{ margin-bottom: 28px; }}

.field-label {{
    display: block;
    font-size: 14px;
    font-weight: 500;
    color: var(--color-text);
    margin-bottom: 10px;
    letter-spacing: -0.005em;
}}

.req {{
    color: var(--color-error);
    margin-left: 3px;
    font-weight: 600;
}}

.field-input,
.field-textarea {{
    width: 100%;
    padding: 16px 18px;
    font-size: 17px;
    font-family: inherit;
    color: var(--color-text);
    background: var(--color-canvas);
    border: 1px solid var(--color-border);
    border-radius: var(--radius);
    outline: none;
    transition: border-color var(--transition), box-shadow var(--transition);
    -webkit-appearance: none;
    appearance: none;
}}

.field-input::placeholder,
.field-textarea::placeholder {{
    color: var(--color-text-subtle);
}}

.field-input:hover,
.field-textarea:hover {{
    border-color: var(--color-border-hover);
}}

.field-input:focus,
.field-textarea:focus {{
    border-color: var(--color-accent);
    box-shadow: var(--shadow-input-focus);
}}

.field-textarea {{
    resize: vertical;
    min-height: 120px;
    line-height: 1.5;
}}

/* Custom Select-Pfeil */
.select-wrap {{ position: relative; }}
.select-wrap::after {{
    content: '';
    position: absolute;
    right: 18px;
    top: 50%;
    width: 10px;
    height: 10px;
    transform: translateY(-70%) rotate(45deg);
    border-right: 1.5px solid var(--color-text-muted);
    border-bottom: 1.5px solid var(--color-text-muted);
    pointer-events: none;
}}
.field-select {{
    padding-right: 44px;
    cursor: pointer;
}}

/* Card-Optionen (Radio / Checkbox als Karten) */
.card-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
    gap: 10px;
}}

.card-option {{
    position: relative;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    padding: 18px 52px 18px 20px;
    background: var(--color-canvas);
    border: 1.5px solid var(--color-border);
    border-radius: var(--radius);
    cursor: pointer;
    transition: all var(--transition);
    user-select: none;
    min-height: 60px;
}}

.card-option:hover {{
    border-color: var(--color-border-hover);
    background: #fcfcfd;
    transform: translateY(-1px);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.04);
}}

.card-option:active {{ transform: translateY(0); }}

.card-radio {{
    position: absolute;
    opacity: 0;
    pointer-events: none;
    width: 0;
    height: 0;
}}

.card-label {{
    font-size: 15px;
    font-weight: 500;
    color: var(--color-text);
    letter-spacing: -0.005em;
}}

/* Checkmark-Indikator rechts */
.card-check {{
    position: absolute;
    right: 16px;
    top: 50%;
    transform: translateY(-50%) scale(0.6);
    width: 22px;
    height: 22px;
    border: 1.5px solid rgba(0, 0, 0, 0.16);
    border-radius: 50%;
    background-color: transparent;
    background-image: none;
    background-position: center;
    background-repeat: no-repeat;
    background-size: 13px 13px;
    opacity: 0;
    transition: all var(--transition);
}}

.card-option:hover .card-check {{
    transform: translateY(-50%) scale(0.85);
    opacity: 0.7;
}}

.card-option:has(.card-radio:checked) {{
    border-color: var(--color-accent);
    background: var(--color-canvas);
    box-shadow: 0 0 0 1px var(--color-accent), 0 4px 14px rgba(0, 0, 0, 0.06);
}}

.card-option:has(.card-radio:checked) .card-check {{
    background-color: var(--color-accent);
    background-image: {_CHECK_SVG};
    border-color: var(--color-accent);
    transform: translateY(-50%) scale(1);
    opacity: 1;
}}

/* Masse-Grid */
.masse-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
}}

.masse-sub {{
    display: block;
    font-size: 13px;
    color: var(--color-text-muted);
    margin-bottom: 6px;
    font-weight: 500;
    letter-spacing: -0.005em;
}}

/* Action-Buttons */
.actions {{
    display: flex;
    gap: 12px;
    margin-top: 48px;
    align-items: center;
}}

.btn {{
    padding: 16px 28px;
    font-size: 16px;
    font-family: inherit;
    font-weight: 500;
    letter-spacing: -0.01em;
    border-radius: var(--radius-pill);
    border: none;
    cursor: pointer;
    transition: all var(--transition);
    line-height: 1;
}}

.btn-primary {{
    background: var(--color-accent);
    color: #fff;
    box-shadow: var(--shadow-button);
    flex: 1;
    min-width: 0;
}}

.btn-primary:hover {{
    background: var(--color-accent-hover);
    transform: translateY(-1px);
    box-shadow: var(--shadow-button-hover);
}}

.btn-primary:active {{
    transform: translateY(0);
    box-shadow: var(--shadow-button);
}}

.btn-secondary {{
    background: transparent;
    color: var(--color-text);
    border: 1px solid var(--color-border-hover);
    flex: 0 0 auto;
}}

.btn-secondary:hover {{
    background: var(--color-tint);
    border-color: rgba(0, 0, 0, 0.28);
}}

/* Footer */
.footer {{
    margin: 80px auto 0;
    max-width: 600px;
    padding: 24px 24px 0;
    font-size: 12px;
    color: var(--color-text-subtle);
    text-align: center;
    line-height: 1.7;
    letter-spacing: -0.005em;
}}

.footer strong {{
    color: var(--color-text-muted);
    font-weight: 600;
}}

/* Mobile */
@media (max-width: 540px) {{
    .page {{ padding: 32px 20px 64px; }}
    h1 {{ font-size: 30px; line-height: 1.12; }}
    .subtitle {{ font-size: 17px; margin-bottom: 32px; }}
    .brand {{ margin-bottom: 36px; }}
    .card-grid {{ grid-template-columns: 1fr 1fr; }}
    .masse-grid {{ grid-template-columns: 1fr; }}
    .actions {{ flex-direction: column-reverse; }}
    .btn {{ width: 100%; }}
    .btn-secondary {{ flex: 1; }}
}}

/* Reduced motion: weniger Bewegung fuer Nutzer mit prefers-reduced-motion */
@media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
    }}
}}

/* ===== Status-Seiten (Erfolg / Fehler / Token-ungueltig) ===== */
.status-page {{
    max-width: 480px;
    margin: 0 auto;
    padding: 120px 24px 80px;
    text-align: center;
}}

.status-icon {{
    margin: 0 auto 36px;
    width: 84px;
    height: 84px;
    display: flex;
    align-items: center;
    justify-content: center;
}}

.status-icon-bg {{
    width: 84px;
    height: 84px;
    border-radius: 50%;
    background: var(--color-accent);
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.12);
    animation: iconPop 600ms var(--ease) 100ms both;
}}

.status-icon-bg.error {{ background: var(--color-error); }}
.status-icon-bg.warn {{ background: #ffb800; }}

@keyframes iconPop {{
    0%   {{ transform: scale(0); opacity: 0; }}
    60%  {{ transform: scale(1.08); opacity: 1; }}
    100% {{ transform: scale(1); opacity: 1; }}
}}

/* SVG check (animiertes Stroke-Drawing) */
.status-svg {{ width: 42px; height: 42px; }}
.status-svg path {{
    stroke: #fff;
    stroke-width: 3.2;
    fill: none;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-dasharray: 60;
    stroke-dashoffset: 60;
    animation: drawStroke 600ms var(--ease) 380ms forwards;
}}

@keyframes drawStroke {{
    to {{ stroke-dashoffset: 0; }}
}}

.status-page h1 {{
    font-size: 40px;
    margin: 0 0 14px;
    animation: fadeUp 500ms var(--ease) 280ms both;
}}

.status-page p {{
    font-size: 19px;
    color: var(--color-text-muted);
    margin: 0 0 8px;
    line-height: 1.42;
    animation: fadeUp 500ms var(--ease) 380ms both;
}}

.status-page p.muted {{
    font-size: 15px;
    color: var(--color-text-subtle);
    margin-top: 28px;
    animation-delay: 460ms;
}}

@keyframes fadeUp {{
    from {{ opacity: 0; transform: translateY(8px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
}}

@media (max-width: 540px) {{
    .status-page {{ padding: 80px 20px 60px; }}
    .status-page h1 {{ font-size: 30px; }}
    .status-page p {{ font-size: 17px; }}
}}
"""

JS = """
(function() {
    const steps = document.querySelectorAll('.step');
    const progressBar = document.getElementById('progress-bar');
    const totalSteps = steps.length;
    let currentStep = 0;

    function updateProgress() {
        const percent = ((currentStep + 1) / totalSteps) * 100;
        progressBar.style.width = percent + '%';
    }

    function showStep(idx) {
        steps.forEach((s, i) => s.classList.toggle('active', i === idx));
        currentStep = idx;
        updateProgress();
        // Smooth scroll, aber nicht bei reduced-motion
        const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
        window.scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' });

        // Erstes Input fokussieren (besseres Mobile-UX, aber kein Auto-Scroll auf iOS)
        if (idx > 0) {
            const firstInput = steps[idx].querySelector(
                'input:not([type=hidden]):not([type=radio]):not([type=checkbox]), textarea, select'
            );
            if (firstInput && !('ontouchstart' in window)) {
                setTimeout(() => firstInput.focus({ preventScroll: true }), 320);
            }
        }
    }

    function flashInvalid(inp) {
        inp.style.borderColor = 'var(--color-error)';
        inp.style.boxShadow = '0 0 0 4px rgba(215, 0, 21, 0.10)';
        setTimeout(() => { inp.style.borderColor = ''; inp.style.boxShadow = ''; }, 1800);
    }

    function validateStep(idx) {
        const step = steps[idx];
        const inputs = step.querySelectorAll(
            'input[required], textarea[required], select[required]'
        );
        for (const inp of inputs) {
            if (inp.type === 'radio') {
                const grp = step.querySelectorAll('input[name="' + inp.name + '"]');
                if (![...grp].some(x => x.checked)) {
                    grp[0].closest('.card-grid')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    return false;
                }
            } else if (inp.type === 'checkbox') {
                // Pflicht-Checkbox (z.B. DSGVO-Einwilligung): value ist immer
                // "on", daher muss explizit auf .checked geprueft werden.
                if (!inp.checked) {
                    inp.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    flashInvalid(inp);
                    return false;
                }
            } else if (!inp.value.trim()) {
                inp.focus({ preventScroll: true });
                inp.scrollIntoView({ behavior: 'smooth', block: 'center' });
                flashInvalid(inp);
                return false;
            }
        }
        return true;
    }

    document.querySelectorAll('[data-action="next"]').forEach(btn => {
        btn.addEventListener('click', () => {
            if (!validateStep(currentStep)) return;
            if (currentStep < totalSteps - 1) showStep(currentStep + 1);
        });
    });
    document.querySelectorAll('[data-action="prev"]').forEach(btn => {
        btn.addEventListener('click', () => {
            if (currentStep > 0) showStep(currentStep - 1);
        });
    });

    // Submit-Button: Letzte Validierung
    const form = document.querySelector('form.page');
    if (form) {
        form.addEventListener('submit', (e) => {
            if (!validateStep(currentStep)) {
                e.preventDefault();
            } else {
                // Submit-Spinner-Feel: Button disablen
                const btn = form.querySelector('button[type="submit"]');
                if (btn) {
                    btn.disabled = true;
                    btn.style.opacity = '0.7';
                    btn.textContent = 'Wird gesendet …';
                }
            }
        });
    }

    showStep(0);
})();
"""


# ============================================================
# Komplettes Form-HTML
# ============================================================

def render_anfrage_form_html(
    schema: dict,
    token: str,
    company_name: str = "",
    branche: str = "",
    preview_mode: bool = False,
) -> str:
    """Komplettes HTML fuer das Multi-Step Anfrage-Formular.

    `preview_mode=True`: zeigt das Formular ohne echten Token zum
    Inspizieren durch den Handwerker (via /formular_anzeigen-Link).
    Der Submit-Button ist disabled, das Form-Action zeigt nicht auf
    /submit, und oben erscheint ein orangefarbenes Vorschau-Banner.
    """
    title = _html.escape(schema.get("title", "Anfrage-Formular"))
    fields = schema.get("fields", [])
    company = _html.escape(company_name or "Anfrage")

    steps = split_fields_into_steps(fields)
    total = len(steps)

    steps_html = []
    for i, step in enumerate(steps):
        is_last = i == total - 1
        is_first = i == 0

        prev_btn = (
            '<button type="button" class="btn btn-secondary" data-action="prev">Zurück</button>'
            if not is_first else ""
        )
        if is_last:
            if preview_mode:
                next_btn = (
                    '<button type="button" class="btn btn-primary" '
                    'disabled title="Vorschau: Absenden deaktiviert">'
                    'Anfrage absenden (Vorschau)</button>'
                )
            else:
                next_btn = (
                    '<button type="submit" class="btn btn-primary">'
                    'Anfrage absenden</button>'
                )
        else:
            next_btn = (
                '<button type="button" class="btn btn-primary" data-action="next">'
                'Weiter</button>'
            )

        fields_html = "\n".join(render_field(f) for f in step["fields"])
        # 01 / 03 Stil
        step_label = f"{i+1:02d} &mdash; {total:02d}"

        # DSGVO: Pflicht-Einwilligung im letzten Schritt (Art. 6/7/13).
        # Die Checkbox wird client- UND serverseitig erzwungen; `_consent`
        # landet in den Antworten und dient als Einwilligungs-Nachweis
        # (Art. 7 Abs. 1, mit submitted_at als Zeitstempel).
        consent_html = ""
        if is_last:
            consent_html = (
                '<label class="consent" style="display:flex;gap:10px;'
                'align-items:flex-start;margin:18px 0 4px;font-size:13px;'
                'line-height:1.5;color:#6e6e73;text-align:left;cursor:pointer;">'
                '<input type="checkbox" name="_consent" required '
                'style="margin-top:3px;flex:0 0 auto;width:18px;height:18px;">'
                f'<span>Ich willige ein, dass {company} die von mir '
                'angegebenen Daten zur Bearbeitung meiner Anfrage verarbeitet. '
                'Zur Missbrauchsvermeidung wird meine IP-Adresse gespeichert. '
                'Meine Rechte (Auskunft, Berichtigung, Löschung) und weitere '
                'Hinweise stehen in der '
                '<a href="https://www.gewerbeagent.de/datenschutz/" '
                'target="_blank" rel="noopener">Datenschutzerklärung</a>. '
                'Die Einwilligung kann ich jederzeit mit Wirkung für die '
                'Zukunft widerrufen.</span></label>'
            )

        steps_html.append(f'''
        <div class="step{' active' if i == 0 else ''}" data-step="{i}">
            <div class="step-counter">{step_label}</div>
            <h1>{_html.escape(step["title"])}</h1>
            <p class="subtitle">{_html.escape(step["subtitle"])}</p>
            {fields_html}
            {consent_html}
            <div class="actions">
                {prev_btn}
                {next_btn}
            </div>
        </div>''')

    # Im Preview-Modus zeigt das Form-Action nicht auf /submit — verhindert
    # versehentliches Absenden auch wenn jemand JS deaktiviert.
    form_action = (
        "javascript:void(0)" if preview_mode
        else f"/anfrage/{_html.escape(token)}/submit"
    )

    preview_banner = ""
    if preview_mode:
        preview_banner = (
            '<div style="background:#fff3cd;border-bottom:1px solid #ffe69c;'
            'padding:12px 20px;text-align:center;font-size:14px;color:#664d03;'
            'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;">'
            '<strong>🔍 Vorschau</strong> &mdash; So sieht das Formular fuer '
            'deinen Kunden aus. Absenden ist deaktiviert.'
            '</div>'
        )

    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f5f5f7">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>

{preview_banner}

<div class="progress-wrap">
    <div class="progress">
        <div id="progress-bar" class="progress-bar"></div>
    </div>
</div>

<form class="page" method="POST" action="{form_action}" autocomplete="off" novalidate enctype="multipart/form-data">

    <div class="brand">{company}</div>

    {"".join(steps_html)}

</form>

<div class="footer">
    Diese Anfrage wird sicher und DSGVO-konform verarbeitet.<br>
    Powered by <strong>Q</strong> &mdash; dein digitaler Handwerks-Assistent.
</div>

<script>{JS}</script>
</body>
</html>"""


# ============================================================
# Status-Seiten (Erfolg / Fehler / Token-ungueltig / Schon abgesendet)
# Eigenes minimal Layout, gleiche Design-Sprache wie das Form.
# ============================================================

_CHECKMARK_SVG = (
    '<svg class="status-svg" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'
)
_CROSS_SVG = (
    '<svg class="status-svg" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M7 7l10 10M17 7L7 17"/></svg>'
)
_INFO_SVG = (
    '<svg class="status-svg" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M12 8v5M12 16.5v0.01"/></svg>'
)


def _render_status_page(
    *,
    title: str,
    headline: str,
    body: str,
    icon: str = "check",  # check | cross | info
    muted_note: str = "",
) -> str:
    """Generisches Status-Seiten-HTML mit gleicher Design-Sprache."""
    if icon == "cross":
        bg_class = "error"
        svg = _CROSS_SVG
    elif icon == "info":
        bg_class = "warn"
        svg = _INFO_SVG
    else:
        bg_class = ""
        svg = _CHECKMARK_SVG

    muted_html = (
        f'<p class="muted">{_html.escape(muted_note)}</p>' if muted_note else ""
    )

    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f5f5f7">
<title>{_html.escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="status-page">
    <div class="status-icon">
        <div class="status-icon-bg {bg_class}">{svg}</div>
    </div>
    <h1>{_html.escape(headline)}</h1>
    <p>{body}</p>
    {muted_html}
</div>
</body>
</html>"""


def render_success_page() -> str:
    return _render_status_page(
        title="Vielen Dank",
        headline="Vielen Dank.",
        body="Deine Anfrage ist bei uns angekommen. Wir melden uns in Kürze mit einem konkreten Vorschlag.",
        icon="check",
        muted_note="Du kannst dieses Fenster jetzt schließen.",
    )


def render_invalid_token_page() -> str:
    return _render_status_page(
        title="Link ungültig",
        headline="Link nicht gültig.",
        body="Der Link ist abgelaufen oder wurde bereits einmal genutzt. Bitte wende dich an den Absender für einen neuen Link.",
        icon="cross",
    )


def render_already_submitted_page() -> str:
    return _render_status_page(
        title="Schon abgesendet",
        headline="Schon erledigt.",
        body="Du hast diese Anfrage bereits ausgefüllt. Wir melden uns gleich bei dir.",
        icon="check",
    )


def render_submit_error_page(message: str) -> str:
    return _render_status_page(
        title="Fehler beim Absenden",
        headline="Das hat nicht geklappt.",
        body=_html.escape(message),
        icon="cross",
        muted_note="Bitte versuche es noch einmal oder melde dich beim Absender.",
    )

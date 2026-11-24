"""Page scripts that pin and drive the Sales Navigator message composer.

The composer (new message and reply alike) is a ``form`` holding one
``textarea[name=message]``, an optional one-line subject ``input`` for an
InMail, and a Send ``button[type=button]`` that stays disabled while the form
is empty. There is no submit-type button and no Press Enter to Send setting,
so the Send control is recognised by structure alone: the one visible
``type=button`` button of the form that is disabled while nothing is typed.
A form where that is not exactly one button is never sent from. The disabled
state of every visible button of the form is recorded when it is pinned, and
Send is clicked only when, after typing, exactly that button went from
disabled to enabled and no other button changed; a button's label is never
read.

``PIN_JS`` records the form's parts on the form element itself, and every
later script works on those recorded elements and refuses when the page no
longer shows exactly them.
"""

_COMMON_JS = r"""
    const visible = el => {
        if (!el || !el.isConnected) return false;
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 &&
            style.visibility !== 'hidden' && style.display !== 'none';
    };
    const editors = () => Array.from(
        document.querySelectorAll('textarea[name="message"]')
    ).filter(visible);
    const subjects = form => Array.from(
        form.querySelectorAll('input[type="text"]')
    ).filter(visible);
    const disabled = button => button.disabled ||
        (button.getAttribute('aria-disabled') || '').toLowerCase() === 'true';
    const validate = owner => {
        const pinned = owner && owner.__linkedinMcpSnComposer;
        if (!pinned || !owner.isConnected) return null;
        const {editor, subject, button} = pinned;
        const shown = editors();
        if (shown.length !== 1 || shown[0] !== editor) return null;
        if (editor.closest('form') !== owner) return null;
        const fields = subjects(owner);
        if (subject ? fields.length !== 1 || fields[0] !== subject : fields.length) {
            return null;
        }
        if (button && (!button.isConnected || !owner.contains(button))) return null;
        return pinned;
    };
    const field = (pinned, name) => name === 'subject' ? pinned.subject : pinned.editor;
    const buttons = form => Array.from(form.querySelectorAll('button')).filter(visible);
    // 'same', 'flipped' (only the Send button went from disabled to enabled)
    // or 'changed' (a button came, went or changed state otherwise).
    const compareButtons = (owner, pinned) => {
        const now = buttons(owner);
        const before = pinned.buttons;
        if (now.length !== before.length) return 'changed';
        let flipped = false;
        for (let index = 0; index < now.length; index++) {
            const [button, was] = before[index];
            if (now[index] !== button) return 'changed';
            const is = disabled(button);
            if (is === was) continue;
            if (button === pinned.button && was && !is) {
                flipped = true;
                continue;
            }
            return 'changed';
        }
        return flipped ? 'flipped' : 'same';
    };
"""

# Returns the form, or null when the page shows no single composer.
PIN_JS = (
    "() => {"
    + "/* linkedin-mcp:sn-composer-pin */"
    + _COMMON_JS
    + r"""
        const shown = editors();
        if (shown.length !== 1) return null;
        const editor = shown[0];
        const form = editor.closest('form');
        if (!form) return null;
        const fields = subjects(form);
        if (fields.length > 1) return null;
        const shownButtons = buttons(form);
        const quiet = shownButtons
            .filter(button => button.type === 'button')
            .filter(disabled);
        form.__linkedinMcpSnComposer = {
            editor,
            subject: fields[0] || null,
            // Only an empty form identifies its Send button; a draft
            // enables it, and such a form is reported occupied anyway.
            button: quiet.length === 1 ? quiet[0] : null,
            sendCount: quiet.length,
            buttons: shownButtons.map(button => [button, disabled(button)]),
            written: {message: '', subject: ''},
        };
        return form;
    }"""
)

STATE_JS = (
    "owner => {"
    + "/* linkedin-mcp:sn-composer-state */"
    + _COMMON_JS
    + r"""
        const pinned = validate(owner);
        if (!pinned || compareButtons(owner, pinned) !== 'same') {
            return {status: 'invalid'};
        }
        return {
            status: 'valid',
            empty: !pinned.editor.value.trim() &&
                !(pinned.subject && pinned.subject.value.trim()),
            subject: !!pinned.subject,
            sendCount: pinned.sendCount,
        };
    }"""
)

# Focuses the field, which must still be empty: 'ready', 'occupied' or
# 'invalid'.
PREPARE_WRITE_JS = (
    "(owner, arg) => {"
    + "/* linkedin-mcp:sn-composer-prepare */"
    + _COMMON_JS
    + r"""
        let pinned = validate(owner);
        const target = pinned && field(pinned, arg.field);
        if (!target) return 'invalid';
        if (target.value.trim()) return 'occupied';
        target.focus();
        pinned = validate(owner);
        if (!pinned || document.activeElement !== target) return 'invalid';
        return target.value.trim() ? 'occupied' : 'ready';
    }"""
)

# Asked before every keystroke: each one goes to whatever holds focus then.
FOCUS_CHECK_JS = (
    "(owner, arg) => {"
    + "/* linkedin-mcp:sn-composer-focus */"
    + _COMMON_JS
    + r"""
        const pinned = validate(owner);
        return !!pinned && document.activeElement === field(pinned, arg.field);
    }"""
)

# The field as a handle, so a line break is pressed on that element itself.
FIELD_JS = (
    "(owner, arg) => {"
    + "/* linkedin-mcp:sn-composer-field */"
    + _COMMON_JS
    + r"""
        const pinned = validate(owner);
        return pinned ? field(pinned, arg.field) : null;
    }"""
)

# Run after typing, also when it stopped partway: the field was empty
# before, so a prefix of the text is this call's own and cleanup may remove
# it. 'written', 'flattened' (the text without its line breaks) or 'invalid'.
SETTLE_WRITE_JS = (
    "(owner, arg) => {"
    + "/* linkedin-mcp:sn-composer-settle */"
    + _COMMON_JS
    + r"""
        const recorded = owner && owner.__linkedinMcpSnComposer;
        const target = recorded && field(recorded, arg.field);
        if (!target) return 'invalid';
        const value = target.value;
        if (arg.text.startsWith(value)) recorded.written[arg.field] = value;
        const pinned = validate(owner);
        if (!pinned) return 'invalid';
        if (value === arg.text) {
            return document.activeElement === target ? 'written' : 'invalid';
        }
        if (
            arg.text.includes('\n') && !value.includes('\n') &&
            value.replace(/\s+/g, '') === arg.text.replace(/\s+/g, '')
        ) {
            recorded.written[arg.field] = value;
            return 'flattened';
        }
        return 'invalid';
    }"""
)

_FILLED_JS = r"""
    const filled = (pinned, arg) =>
        document.activeElement === pinned.editor &&
        pinned.editor.value === arg.message &&
        (pinned.subject ? pinned.subject.value === arg.subject : arg.subject === null);
"""

# 'ready', 'disabled' or 'invalid'.
SUBMIT_READY_JS = (
    "(owner, arg) => {"
    + "/* linkedin-mcp:sn-composer-ready */"
    + _COMMON_JS
    + _FILLED_JS
    + r"""
        const pinned = validate(owner);
        if (!pinned || !pinned.button || !filled(pinned, arg)) return 'invalid';
        const buttonsNow = compareButtons(owner, pinned);
        if (buttonsNow === 'changed') return 'invalid';
        return buttonsNow === 'flipped' ? 'ready' : 'disabled';
    }"""
)

SUBMIT_JS = (
    "(owner, arg) => {"
    + "/* linkedin-mcp:sn-composer-send */"
    + _COMMON_JS
    + _FILLED_JS
    + r"""
        const pinned = validate(owner);
        if (
            !pinned || !pinned.button || !filled(pinned, arg) ||
            compareButtons(owner, pinned) !== 'flipped'
        ) {
            return 'invalid';
        }
        pinned.button.click();
        return 'clicked';
    }"""
)

# Empties only what this call typed: a field whose value is still exactly
# the recorded text. Anything else belongs to someone else.
CLEANUP_JS = r"""owner => {
    /* linkedin-mcp:sn-composer-cleanup */
    const pinned = owner && owner.__linkedinMcpSnComposer;
    if (!pinned || !owner.isConnected) return false;
    let cleared = false;
    for (const [name, target] of [['message', pinned.editor], ['subject', pinned.subject]]) {
        const typed = pinned.written[name];
        if (!target || !target.isConnected || !typed || target.value !== typed) continue;
        const prototype = Object.getPrototypeOf(target);
        Object.getOwnPropertyDescriptor(prototype, 'value').set.call(target, '');
        target.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
        pinned.written[name] = '';
        cleared = true;
    }
    return cleared;
}"""

DISPOSE_JS = r"""owner => {
    /* linkedin-mcp:sn-composer-dispose */
    if (owner) delete owner.__linkedinMcpSnComposer;
}"""

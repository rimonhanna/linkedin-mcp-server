"""LinkedIn's "Press Enter to Send" preference, as send and reply see it.

With the preference on, a composer has no submit button, and sending would
take a plain Enter keypress, which these tools never press (line breaks are
Shift+Enter). ``send_message`` and ``reply_to_conversation`` therefore refuse
with ``ENTER_TO_SEND_ENABLED`` before anything is typed, so the caller learns
which setting to change instead of retrying into a generic
``send_unavailable``. The detection is kept apart from ``message_sender`` so
both paths, and their tests, read one definition of the signal.
"""

ENTER_TO_SEND_ENABLED = "enter_to_send_enabled"

ENTER_TO_SEND_MESSAGE = (
    "No Send button was found in the composer, most likely because LinkedIn's "
    "Press Enter to Send setting is on. Nothing was typed or sent. Turn the "
    "setting off in LinkedIn messaging, then try again."
)

# A JS declaration for inlining into a composer inspection, beside its own
# `visible`. The preference is recognized structurally (CLAUDE.md scraping
# rules forbid layout classes such as upstream's `.msg-form__send-toggle`):
# the editor's own form shows no submit button but does show the
# send-options toggle, a button that opens a menu and so carries
# `aria-expanded` or `aria-haspopup`. It fails safe: a form this misreads
# still has no submit button, and a send path refuses it either way.
ENTER_TO_SEND_JS = """
    const enterToSendOn = (scope, submitCount, visible) =>
        submitCount === 0 &&
        !!scope &&
        scope.matches('form') &&
        Array.from(
            scope.querySelectorAll('button[aria-expanded], button[aria-haspopup]')
        ).some(visible);
"""

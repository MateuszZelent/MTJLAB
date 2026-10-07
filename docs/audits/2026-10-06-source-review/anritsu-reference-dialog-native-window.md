# Anritsu reference dialog: native Windows regression

## Reproduced failure

The reference and recording dialogs were constructed with the Anritsu page
as their parent, before the page was inserted into the Fluent application shell.
`StationDialog` uses `FramelessDialog`, which eagerly creates a native HWND.
Embedding the page destroys its former native window; Windows also destroys
the dialogs owned by it. Qt retains their stale IDs and can report
`isVisible() == True` after a button click, even though `win32gui.IsWindow()`
returns false. This explains why the previous offscreen visibility tests passed.

The background assistant is constructed on demand, after page insertion, and
does not follow this failing path.

## Correction

- Construct eager workflow dialogs without the temporary page owner.
- Connect page destruction to dialog deletion, including dialogs never opened.
- On opening, assign the current top-level Fluent window as their Qt parent
  and native transient owner, before showing them.
- Retain compact-flyout dismissal, screen positioning and minimized restoration.
- Report reference opening in the status log and display synchronous opening
  exceptions as an error notification.

Opening configuration dispatches no instrument operations.

## Native verification

`tests/test_reference_setup_native_owner.py` runs with
`QT_QPA_PLATFORM=windows`, using zero opacity to avoid displaying test windows
while retaining actual HWNDs. Both reference and recording cases pass:
native window existence after shell insertion, native visibility after clicking,
`GW_OWNER` equality with the main window, minimized restoration, closing and
reopening, and no controller dispatch. Result: **2 passed**.

The offscreen shell regression additionally checks rendered dialog geometry,
the compact controls path and screenshots at desktop and smaller window sizes.
Native tests skip when the Windows Qt backend is unavailable.

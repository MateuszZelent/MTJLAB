# UI Migration Contract

## Fluent-first application shell

The application UI is undergoing a **full production migration** to
PySide6-Fluent-Widgets. This is a product-quality redesign, not a visual skin.

- Do not embed, wrap, or hide a legacy `QMainWindow`, `QTabWidget`, ribbon, or
  other legacy shell inside a Fluent window.
- Do not retain compatibility facades or temporary adapters in the UI shell.
  Migrate the caller to the Fluent-native API instead.
- The top-level window, navigation, page hosting, title bar, settings route,
  status presentation, and safety presentation must use one coherent
  Fluent-native layout tree.
- Standard Qt widgets remain acceptable *inside an individual page* where they
  are functional controls; the application shell itself must not be hybrid.
- A page must be a visible, correctly parented child of the Fluent content
  layout. Tests for shell work must verify rendered geometry after `show()` and
  event processing, not only object identity or route selection.

## Visual quality bar

Use the QFluent design language deliberately: clear hierarchy, stable spacing,
semantic colours, readable typography, restrained elevation, responsive
navigation, and calm safety affordances. Reuse the project design tokens; do
not add per-page ad-hoc styles that fight the application theme.

- Prefer purpose-built PySide6-Fluent-Widgets controls over hand-styled basic
  Qt controls whenever a Fluent control expresses the interaction better.
- Use QFluentWidgets Pro components wherever they materially improve the
  product and the installed package and project licence permit their use;
  otherwise use the closest standard Fluent component. Do not create a rough
  custom imitation of an available Fluent/Pro element.
- Treat micro-details as product requirements: empty, loading, disabled,
  hover, focus, error and narrow-window states; icon alignment; text density;
  keyboard access; transitions; and light/dark-theme contrast all need
  intentional review.

## Delivery rule

For each migration slice, preserve existing user workflows and safety actions,
then add a focused automated rendering/regression test. Do not call a slice
complete until it is both functionally verified and visually inspectable at a
normal desktop window size.

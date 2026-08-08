# Clean-room protocol provenance

Copyright 2026 Rujun Wang

The distributed firmware, entity package, register CSV, analyzer, tests and
documentation do not contain or import third-party MakeSkyBlue protocol files.

Published protocol facts are independently expressed from this project's
RX-only captures and, where identified in the register CSV, corroborated
against public product-manual or IoTRix UI labels. No third-party code,
protocol table, APK asset, or manual page is redistributed. D-registers
without sufficient evidence remain named `observed_raw_u16`; provisional
fields stay explicit until a live value cross-check is complete, and a zero
sample is never treated as proof that a word is reserved.

#pragma once

#if defined(RTS_REPLAY_ANALYZER) && defined(_MSC_VER) && !defined(IS_VS6_BUILD)
// TheSuperHackers @performance Leex 25/08/2026 Optimize analyzer observers with modern MSVC without changing legacy or MinGW builds. (#TBD)
#pragma optimize("gt", on)
#endif

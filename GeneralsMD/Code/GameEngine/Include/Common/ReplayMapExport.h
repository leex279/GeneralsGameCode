#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/AsciiString.h"

class Object;
struct Coord3D;

// TheSuperHackers @feature Leex 21/08/2026 Export one immutable initialized map snapshot for replay analytics. (#TBD)
class ReplayMapExport
{
public:
	static void reset();
	static Bool prepare();
	static const AsciiString &referenceJson();
	// TheSuperHackers @feature Leex 21/08/2026 Share the exact catalog-derived static-feature classification with bounds-policy emission. (#TBD)
	static Bool isClassifiedStaticObject(const Object *object);
	// TheSuperHackers @bugfix Leex 24/08/2026 Authorize visual-debris bounds exemptions only in the exported two-cell edge margin. (#TBD)
	static Bool needsTrustedVisualDebrisPositionExemption(const Coord3D *position);
	// TheSuperHackers @bugfix Leex 24/08/2026 Identify trusted visual debris that has traveled beyond the bounded telemetry envelope. (#TBD)
	static Bool isBeyondTrustedVisualDebrisPositionMargin(const Coord3D *position);
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

// TheSuperHackers @feature Leex 23/08/2026 Export bounded side-effect-free scouting observations from authoritative partition shroud state. (#0)
class ReplayVisibilitySampler
{
public:
	static void reset();
	static void sampleEndOfFrame();
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

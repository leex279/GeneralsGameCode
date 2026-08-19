#pragma once

#if defined(RTS_REPLAY_ANALYZER)

#include "Common/AsciiString.h"
#include "Common/MessageStream.h"
#include "Common/Recorder.h"

/**
 * Process-local observer for the serialized replay stream. It deliberately owns no simulation state.
 */
class ReplayParseDump
{
public:
	static void setOutputPath(const AsciiString &path);
	static Bool isEnabled();
	static Bool beginReplay(const RecorderClass::ReplayHeader &header, Int endOffset);
	static void writeSetup(Int difficulty, Int originalGameMode, Int rankPoints, Int maxFPS, Int startOffset, Int endOffset);
	static void writeCommand(Int frame, Int startOffset, Int endOffset, const GameMessage &message);
	static void writeMessageCatalog();
	static void finishReplay(Int endOffset, Bool complete);
};

#endif // defined(RTS_REPLAY_ANALYZER)

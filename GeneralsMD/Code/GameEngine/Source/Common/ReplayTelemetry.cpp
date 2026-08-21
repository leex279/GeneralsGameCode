#include "PreRTS.h"

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/ReplayTelemetry.h"

#include "Common/GlobalData.h"
#include "Common/ReplayCombat.h"
#include "Common/ReplayGameDataExport.h"
#include "Common/ReplayMapExport.h"
#include "Common/ReplayMovementSampler.h"
#include "Common/ReplayOutcome.h"
#include "Common/ReplayEconomy.h"
#include "Common/ReplayEntityLifecycle.h"
#include "Common/version.h"
#include "GameNetwork/GameInfo.h"

#include <array>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>

namespace
{
	class Sha256
	{
	public:
		Sha256() :
			m_state({ 0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
				0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U }),
			m_block(),
			m_blockSize(0),
			m_totalBytes(0)
		{
		}

		void update(const char *data, size_t length)
		{
			const UnsignedByte *bytes = reinterpret_cast<const UnsignedByte *>(data);
			m_totalBytes += static_cast<unsigned long long>(length);
			while (length > 0)
			{
				const size_t available = m_block.size() - m_blockSize;
				const size_t copyLength = length < available ? length : available;
				memcpy(m_block.data() + m_blockSize, bytes, copyLength);
				m_blockSize += copyLength;
				bytes += copyLength;
				length -= copyLength;
				if (m_blockSize == m_block.size())
				{
					transform(m_block.data());
					m_blockSize = 0;
				}
			}
		}

		std::string hexDigest() const
		{
			Sha256 digest = *this;
			const unsigned long long totalBits = static_cast<unsigned long long>(digest.m_totalBytes) * 8ULL;
			digest.m_block[digest.m_blockSize++] = 0x80;
			if (digest.m_blockSize > 56)
			{
				while (digest.m_blockSize < digest.m_block.size())
				{
					digest.m_block[digest.m_blockSize++] = 0;
				}
				digest.transform(digest.m_block.data());
				digest.m_blockSize = 0;
			}
			while (digest.m_blockSize < 56)
			{
				digest.m_block[digest.m_blockSize++] = 0;
			}
			for (Int i = 7; i >= 0; --i)
			{
				digest.m_block[digest.m_blockSize++] = static_cast<UnsignedByte>(totalBits >> (i * 8));
			}
			digest.transform(digest.m_block.data());

			static const char hex[] = "0123456789abcdef";
			std::string result;
			result.reserve(64);
			for (UnsignedInt value : digest.m_state)
			{
				for (Int shift = 28; shift >= 0; shift -= 4)
				{
					result.push_back(hex[(value >> shift) & 0x0f]);
				}
			}
			return result;
		}

	private:
		static UnsignedInt rotateRight(UnsignedInt value, UnsignedInt bits)
		{
			return (value >> bits) | (value << (32 - bits));
		}

		void transform(const UnsignedByte *block)
		{
			static const UnsignedInt constants[64] = {
				0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U, 0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
				0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U, 0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
				0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU, 0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
				0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U, 0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
				0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U, 0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
				0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U, 0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
				0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U, 0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
				0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U, 0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U
			};
			UnsignedInt words[64];
			for (Int i = 0; i < 16; ++i)
			{
				const Int offset = i * 4;
				words[i] = (static_cast<UnsignedInt>(block[offset]) << 24)
					| (static_cast<UnsignedInt>(block[offset + 1]) << 16)
					| (static_cast<UnsignedInt>(block[offset + 2]) << 8)
					| static_cast<UnsignedInt>(block[offset + 3]);
			}
			for (Int i = 16; i < 64; ++i)
			{
				const UnsignedInt s0 = rotateRight(words[i - 15], 7) ^ rotateRight(words[i - 15], 18) ^ (words[i - 15] >> 3);
				const UnsignedInt s1 = rotateRight(words[i - 2], 17) ^ rotateRight(words[i - 2], 19) ^ (words[i - 2] >> 10);
				words[i] = words[i - 16] + s0 + words[i - 7] + s1;
			}

			UnsignedInt a = m_state[0];
			UnsignedInt b = m_state[1];
			UnsignedInt c = m_state[2];
			UnsignedInt d = m_state[3];
			UnsignedInt e = m_state[4];
			UnsignedInt f = m_state[5];
			UnsignedInt g = m_state[6];
			UnsignedInt h = m_state[7];
			for (Int i = 0; i < 64; ++i)
			{
				const UnsignedInt sum1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
				const UnsignedInt choice = (e & f) ^ ((~e) & g);
				const UnsignedInt temp1 = h + sum1 + choice + constants[i] + words[i];
				const UnsignedInt sum0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
				const UnsignedInt majority = (a & b) ^ (a & c) ^ (b & c);
				const UnsignedInt temp2 = sum0 + majority;
				h = g;
				g = f;
				f = e;
				e = d + temp1;
				d = c;
				c = b;
				b = a;
				a = temp1 + temp2;
			}
			m_state[0] += a;
			m_state[1] += b;
			m_state[2] += c;
			m_state[3] += d;
			m_state[4] += e;
			m_state[5] += f;
			m_state[6] += g;
			m_state[7] += h;
		}

		std::array<UnsignedInt, 8> m_state;
		std::array<UnsignedByte, 64> m_block;
		size_t m_blockSize;
		unsigned long long m_totalBytes;
	};

	FILE *s_output = nullptr;
	AsciiString s_tracePath;
	AsciiString s_tempPath;
	AsciiString s_runId;
	AsciiString s_engineDataIdentity;
	AsciiString s_catalogPath;
	AsciiString s_catalogSha256;
	AsciiString s_catalogEngineDataIdentity;
	AsciiString s_replayVersion;
	AsciiString s_mapIdentity;
	Int s_initialSeed = 0;
	Int s_replayLocalSlotIndex = -1;
	Int s_movementSampleFrames = 15;
	unsigned long long s_sequence = 0;
	unsigned long long s_commandCount = 0;
	UnsignedInt s_eventsSinceFlush = 0;
	std::map<std::string, unsigned long long> s_eventCounts;
	Sha256 s_traceDigest;
	AsciiString s_writerError;
	Bool s_outputFailed = FALSE;
	Bool s_initialized = FALSE;
	Bool s_finishDeferred = FALSE;
	ReplayTelemetryTerminationReason s_deferredTerminationReason = REPLAY_TELEMETRY_TERMINATION_INTERRUPTED;
	Bool s_ownsTempPath = FALSE;
	UnsignedInt s_tempCounter = 0;
	char s_outputBuffer[64 * 1024];

	void setWriterError(const char *code, const char *message, Bool outputFailure = FALSE);

	std::string jsonString(const char *value)
	{
		std::string result = "\"";
		const UnsignedByte *cursor = reinterpret_cast<const UnsignedByte *>(value != nullptr ? value : "");
		while (*cursor != 0)
		{
			const UnsignedByte character = *cursor++;
			switch (character)
			{
			case '"': result += "\\\""; break;
			case '\\': result += "\\\\"; break;
			case '\b': result += "\\b"; break;
			case '\f': result += "\\f"; break;
			case '\n': result += "\\n"; break;
			case '\r': result += "\\r"; break;
			case '\t': result += "\\t"; break;
			default:
				if (character < 0x20)
				{
					char escaped[7];
					const Int escapedLength = snprintf(escaped, sizeof(escaped), "\\u%04x", static_cast<UnsignedInt>(character));
					if (escapedLength == 6)
					{
						result.append(escaped, static_cast<size_t>(escapedLength));
					}
					else
					{
						setWriterError("format_failed", "could not format a JSON escape", TRUE);
						result += "\\ufffd";
					}
				}
				else if (character < 0x80)
				{
					result.push_back(static_cast<char>(character));
				}
				else
				{
					// TheSuperHackers @feature Leex 18/08/2026 Preserve narrow engine text as valid UTF-8 JSON. (#TBD)
					result.push_back(static_cast<char>(0xc0 | (character >> 6)));
					result.push_back(static_cast<char>(0x80 | (character & 0x3f)));
				}
				break;
			}
		}
		result.push_back('"');
		return result;
	}

	std::string jsonString(const AsciiString &value)
	{
		return jsonString(value.str());
	}

	std::string logicTime(UnsignedInt frame)
	{
		char value[64];
		const Int valueLength = snprintf(value, sizeof(value), "%.17g", static_cast<double>(frame) / 30.0);
		if (valueLength <= 0 || valueLength >= static_cast<Int>(sizeof(value)))
		{
			setWriterError("format_failed", "could not format telemetry logic time", TRUE);
			return "0";
		}
		return value;
	}

	std::string envelope(unsigned long long sequence, UnsignedInt frame, const char *eventType, const std::string &payload)
	{
		return "{\"schema_version\":2,\"run_id\":" + jsonString(s_runId)
			+ ",\"sequence\":" + std::to_string(sequence)
			+ ",\"frame\":" + std::to_string(frame)
			+ ",\"logic_time_seconds\":" + logicTime(frame)
			+ ",\"event_type\":" + jsonString(eventType)
			+ ",\"payload\":" + payload + "}\n";
	}

	void setWriterError(const char *code, const char *message, Bool outputFailure)
	{
		s_outputFailed = s_outputFailed || outputFailure;
		AsciiString detail;
		detail.format("%s: %s", code != nullptr ? code : "unknown", message != nullptr ? message : "unknown error");
		if (s_writerError.isEmpty())
		{
			s_writerError = detail;
		}
		fprintf(stderr, "ReplayTelemetry: %s\n", detail.str());
		fflush(stderr);
	}

	void writeLine(const std::string &line, Bool includeInDigest)
	{
		if (s_output == nullptr)
		{
			return;
		}
		const size_t written = fwrite(line.data(), 1, line.size(), s_output);
		if (includeInDigest && written > 0)
		{
			s_traceDigest.update(line.data(), written);
		}
		if (written != line.size())
		{
			setWriterError("write_failed", "could not write the complete telemetry record", TRUE);
		}
	}

	void flushOutput()
	{
		if (s_output != nullptr && fflush(s_output) != 0)
		{
			setWriterError("flush_failed", "could not flush telemetry output", TRUE);
		}
		s_eventsSinceFlush = 0;
	}

	std::string eventCountsJson()
	{
		std::string result = "{";
		Bool first = TRUE;
		for (const auto &entry : s_eventCounts)
		{
			if (!first)
			{
				result.push_back(',');
			}
			first = FALSE;
			result += jsonString(entry.first.c_str()) + ":" + std::to_string(entry.second);
		}
		result.push_back('}');
		return result;
	}

	void discardTemporaryOutput()
	{
		if (s_ownsTempPath && s_tempPath.isNotEmpty())
		{
			AsciiString discardedPath = s_tempPath;
			errno = 0;
			if (remove(discardedPath.str()) != 0 && errno != ENOENT)
			{
				AsciiString message;
				message.format("could not remove transaction '%s'", discardedPath.str());
				setWriterError("cleanup_failed", message.str(), TRUE);
			}
		}
		s_ownsTempPath = FALSE;
		s_tempPath.clear();
	}

	void discardPendingOutput(const char *closeFailureMessage)
	{
		// TheSuperHackers @feature Leex 18/08/2026 Discard an unpublished v2 transaction when authoritative initialization cannot complete. (#TBD)
		if (s_output != nullptr)
		{
			FILE *output = s_output;
			s_output = nullptr;
			if (fclose(output) != 0)
			{
				setWriterError("close_failed", closeFailureMessage, TRUE);
			}
		}
		s_initialized = FALSE;
		discardTemporaryOutput();
	}

	void publishTemporaryOutput()
	{
		if (!s_ownsTempPath || s_tempPath.isEmpty())
		{
			setWriterError("publish_failed", "telemetry transaction is not owned by this writer", TRUE);
			return;
		}
		// TheSuperHackers @feature Leex 18/08/2026 Publish with no replacement so a destination created during playback remains untouched. (#TBD)
		if (!MoveFileA(s_tempPath.str(), s_tracePath.str()))
		{
			setWriterError("publish_failed", "could not exclusively publish telemetry output", TRUE);
			discardTemporaryOutput();
			return;
		}
		s_ownsTempPath = FALSE;
		s_tempPath.clear();
	}
}

void ReplayTelemetry::configure(const AsciiString &tracePath, const AsciiString &runId, Int movementSampleFrames)
{
	if (s_output != nullptr)
	{
		ReplayTelemetry::discard();
	}
	s_tracePath = tracePath;
	s_runId = runId;
	s_movementSampleFrames = movementSampleFrames;
	s_finishDeferred = FALSE;
	s_engineDataIdentity.clear();
	s_catalogPath.clear();
	s_catalogSha256.clear();
	s_catalogEngineDataIdentity.clear();
	s_replayVersion.clear();
	s_mapIdentity.clear();
	s_initialSeed = 0;
	s_replayLocalSlotIndex = -1;
	s_initialized = FALSE;
	ReplayGameDataExport::reset();
	// TheSuperHackers @feature Leex 21/08/2026 Reset the trace-local authoritative map reference before replay initialization. (#TBD)
	ReplayMapExport::reset();
	// TheSuperHackers @feature Leex 20/08/2026 Reset trace-local combat and terminal observations before a new replay. (#TBD)
	ReplayCombat::reset();
	// TheSuperHackers @feature Leex 20/08/2026 Reset trace-local economy and queue identities before a new replay. (#TBD)
	ReplayEconomy::reset();
	// TheSuperHackers @feature Leex 20/08/2026 Reset trace-local entity snapshots whenever telemetry is reconfigured. (#TBD)
	ReplayEntityLifecycle::reset();
	// TheSuperHackers @feature Leex 20/08/2026 Reset copied movement and order observations whenever telemetry is reconfigured. (#TBD)
	ReplayMovementSampler::reset();
}

Bool ReplayTelemetry::isEnabled()
{
	return s_tracePath.isNotEmpty();
}

Bool ReplayTelemetry::isInitialized()
{
	return s_initialized;
}

const AsciiString &ReplayTelemetry::getTracePath()
{
	return s_tracePath;
}

const AsciiString &ReplayTelemetry::getEngineDataIdentity()
{
	return s_engineDataIdentity;
}

const AsciiString &ReplayTelemetry::getMapIdentity()
{
	return s_mapIdentity;
}

Int ReplayTelemetry::getReplayLocalSlotIndex()
{
	return s_replayLocalSlotIndex;
}

// TheSuperHackers @feature Leex 20/08/2026 Expose the validated sampling bound to the passive end-of-frame observer. (#TBD)
Int ReplayTelemetry::getMovementSampleFrames()
{
	return s_movementSampleFrames;
}

AsciiString ReplayTelemetry::sha256Hex(const char *data, size_t length)
{
	Sha256 digest;
	digest.update(data, length);
	return AsciiString(digest.hexDigest().c_str());
}

void ReplayTelemetry::setGameDataCatalog(const AsciiString &path, const AsciiString &sha256,
	const AsciiString &engineDataIdentity)
{
	// TheSuperHackers @feature Leex 18/08/2026 Bind the manifest to one validated content-addressed engine-data asset. (#TBD)
	s_catalogPath = path;
	s_catalogSha256 = sha256;
	s_catalogEngineDataIdentity = engineDataIdentity;
}

void ReplayTelemetry::begin(const RecorderClass::ReplayHeader &header)
{
	if (!isEnabled() || s_output != nullptr)
	{
		return;
	}
	ReplayCombat::reset();

	s_sequence = 0;
	s_commandCount = 0;
	s_eventsSinceFlush = 0;
	s_eventCounts.clear();
	s_traceDigest = Sha256();
	s_writerError.clear();
	s_outputFailed = FALSE;
	s_finishDeferred = FALSE;
	s_ownsTempPath = FALSE;
	s_tempPath.clear();
	for (Int attempt = 0; attempt < 100 && s_output == nullptr; ++attempt)
	{
		AsciiString candidatePath;
		candidatePath.format("%s.tmp.%lu.%u", s_tracePath.str(), static_cast<unsigned long>(GetCurrentProcessId()), ++s_tempCounter);
		errno = 0;
		s_output = fopen(candidatePath.str(), "wbx");
		if (s_output != nullptr)
		{
			// TheSuperHackers @feature Leex 18/08/2026 Own cleanup only after this writer successfully creates the exclusive transaction. (#TBD)
			s_tempPath = candidatePath;
			s_ownsTempPath = TRUE;
		}
		if (s_output == nullptr && errno != EEXIST)
		{
			break;
		}
	}
	if (s_output == nullptr)
	{
		AsciiString message;
		message.format("could not create a transaction for '%s'", s_tracePath.str());
		setWriterError("open_failed", message.str(), TRUE);
		discardTemporaryOutput();
		return;
	}
	setvbuf(s_output, s_outputBuffer, _IOFBF, sizeof(s_outputBuffer));

	s_engineDataIdentity.format("zero-hour-%u-exe-%08X-ini-%08X", TheVersion->getVersionNumber(), TheGlobalData->m_exeCRC,
		TheGlobalData->m_iniCRC);
	s_replayVersion.translate(header.versionString);
	if (s_replayVersion.isEmpty())
	{
		s_replayVersion.format("%u", header.versionNumber);
	}
	s_mapIdentity = header.filename;
	s_initialSeed = 0;
	s_replayLocalSlotIndex = header.localPlayerIndex;
	// TheSuperHackers @feature Leex 20/08/2026 Preserve replay terminal metadata separately from observed executed player transitions. (#TBD)
	ReplayCombat::observeReplayHeader(header);
	if (TheRecorder != nullptr && TheRecorder->getGameInfo() != nullptr)
	{
		s_mapIdentity = TheRecorder->getGameInfo()->getMap();
		s_initialSeed = TheRecorder->getGameInfo()->getSeed();
	}
	// TheSuperHackers @feature Leex 18/08/2026 Keep the trace unpublished until map overrides and replay players are authoritative. (#TBD)
}

void ReplayTelemetry::initialize()
{
	if (s_output == nullptr || s_initialized)
	{
		return;
	}
	// TheSuperHackers @feature Leex 18/08/2026 Publish v2 provenance only from the post-map authoritative initialization seam. (#TBD)
	// TheSuperHackers @feature Leex 21/08/2026 Validate and atomically publish the initialized map before manifest record zero. (#TBD)
	if (!ReplayMapExport::prepare() || !ReplayGameDataExport::prepareCatalog() || s_outputFailed)
	{
		discardPendingOutput("could not close telemetry output after authoritative initialization failure");
		return;
	}
	const Bool audioEnabled = TheGlobalData != nullptr && TheGlobalData->m_audioOn;
	// TheSuperHackers @feature Leex 20/08/2026 Bind explicit supported-order coverage and movement density to the v2 manifest. (#TBD)
	const std::string payload = "{\"engine_build\":" + jsonString(s_engineDataIdentity)
		+ ",\"replay_version\":" + jsonString(s_replayVersion)
		+ ",\"map_identity\":" + jsonString(s_mapIdentity)
		+ ",\"initial_seed\":" + std::to_string(s_initialSeed)
		+ ",\"exporter_settings\":{\"movement_sample_frames\":" + std::to_string(s_movementSampleFrames)
		+ ",\"audio_enabled\":" + (audioEnabled ? "true" : "false")
		+ ",\"order_coverage\":" + ReplayMovementSampler::orderCoverageJson().str() + "}"
		+ ",\"game_data_catalog\":{\"type\":\"game_data_catalog\",\"path\":" + jsonString(s_catalogPath)
		+ ",\"sha256\":" + jsonString(s_catalogSha256)
		+ ",\"engine_data_identity\":" + jsonString(s_catalogEngineDataIdentity) + "}"
		+ ",\"map_asset\":" + ReplayMapExport::referenceJson().str() + "}";
	writeLine(envelope(s_sequence++, 0, "manifest", payload), TRUE);
	s_eventCounts["manifest"] = 1;
	flushOutput();
	if (s_outputFailed)
	{
		discardPendingOutput("could not close telemetry output after manifest failure");
		return;
	}
	s_initialized = TRUE;
	ReplayGameDataExport::emitPlayersInitialized();
	// TheSuperHackers @feature Leex 20/08/2026 Freeze the authoritative player domain after publishing the matching snapshot. (#TBD)
	ReplayCombat::initialize();
	// TheSuperHackers @feature Leex 20/08/2026 Preserve manifest-first ordering while flushing pre-initialization entity snapshots. (#TBD)
	ReplayEntityLifecycle::initialize();
	// TheSuperHackers @feature Leex 20/08/2026 Flush immutable pre-initialization cash only after entity creation evidence. (#TBD)
	ReplayEconomy::initialize();
	if (s_outputFailed)
	{
		discardPendingOutput("could not close telemetry output after player snapshot failure");
	}
}

void ReplayTelemetry::emit(UnsignedInt frame, const char *eventType, const AsciiString &payloadJson)
{
	if (s_output == nullptr || !s_initialized)
	{
		return;
	}
	writeLine(envelope(s_sequence++, frame, eventType, payloadJson.str()), TRUE);
	++s_eventCounts[eventType != nullptr ? eventType : ""];
	if (++s_eventsSinceFlush >= 256)
	{
		flushOutput();
	}
}

void ReplayTelemetry::observeExecutedCommand()
{
	// TheSuperHackers @feature Leex 20/08/2026 Count the same executed replay-command seam for telemetry-independent parity evidence. (#TBD)
	ReplayOutcome::observeExecutedCommand();
	if (s_output != nullptr)
	{
		++s_commandCount;
	}
}

void ReplayTelemetry::deferFinish(ReplayTelemetryTerminationReason reason)
{
	if ((s_output != nullptr && s_initialized) || ReplayOutcome::isEnabled())
	{
		s_finishDeferred = TRUE;
		s_deferredTerminationReason = reason;
	}
}

void ReplayTelemetry::finishDeferred(UnsignedInt finalFrame)
{
	if (s_finishDeferred)
	{
		// TheSuperHackers @feature Leex 18/08/2026 Recheck CRC after the terminal logic update before publishing clean completion. (#TBD)
		const ReplayTelemetryTerminationReason reason = TheRecorder != nullptr && TheRecorder->sawCRCMismatch()
			? REPLAY_TELEMETRY_TERMINATION_CRC_MISMATCH : s_deferredTerminationReason;
		finish(finalFrame, reason);
	}
}

void ReplayTelemetry::finish(UnsignedInt finalFrame, ReplayTelemetryTerminationReason reason)
{
	s_finishDeferred = FALSE;
	// TheSuperHackers @feature Leex 20/08/2026 Publish independent terminal facts before telemetry writer state can short-circuit. (#TBD)
	ReplayOutcome::finish(finalFrame, reason);
	if (s_output == nullptr)
	{
		return;
	}
	if (!s_initialized)
	{
		discardPendingOutput("could not close telemetry output before authoritative initialization");
		return;
	}

	// TheSuperHackers @feature Leex 20/08/2026 Emit exactly one authoritative outcome immediately before trace completion. (#TBD)
	ReplayCombat::emitMatchOutcome(finalFrame, reason);
	flushOutput();
	++s_eventCounts["complete"];
	const std::string writerError = s_writerError.isEmpty() ? "null" : jsonString(s_writerError);
	// TheSuperHackers @feature Leex 20/08/2026 Reconcile every observed cash chain against terminal engine Money state. (#TBD)
	const AsciiString finalCashBalances = ReplayEconomy::finalCashBalancesJson();
	const AsciiString combatCompletionFields = ReplayCombat::completionFieldsJson(reason);
	const Bool crcMismatch = reason == REPLAY_TELEMETRY_TERMINATION_CRC_MISMATCH;
	const Bool replayTruncated = reason == REPLAY_TELEMETRY_TERMINATION_TRUNCATED_INPUT;
	const Bool cleanShutdown = reason == REPLAY_TELEMETRY_TERMINATION_CLEAN_EOF;
	const std::string payload = "{\"final_frame\":" + std::to_string(finalFrame)
		+ ",\"command_count\":" + std::to_string(s_commandCount)
		+ ",\"event_counts\":" + eventCountsJson()
		+ ",\"crc_mismatch\":" + (crcMismatch ? "true" : "false")
		+ "," + combatCompletionFields.str()
		+ ",\"replay_truncated\":" + (replayTruncated ? "true" : "false")
		+ ",\"clean_shutdown\":" + (cleanShutdown ? "true" : "false")
		+ ",\"writer_error\":" + writerError
		+ ",\"trace_sha256\":\"" + s_traceDigest.hexDigest()
		+ "\",\"map_assets\":[" + ReplayMapExport::referenceJson().str()
		+ "],\"final_cash_balances\":" + finalCashBalances.str() + "}";
	writeLine(envelope(s_sequence++, finalFrame, "complete", payload), FALSE);
	// TheSuperHackers @feature Leex 18/08/2026 Exercise late transaction failure without feeding the result into replay execution. (#TBD)
	const char *injectedFailure = getenv("GENERALS_REPLAY_TELEMETRY_TEST_FAIL_AFTER_COMPLETE_WRITE");
	if (injectedFailure != nullptr && strcmp(injectedFailure, "1") == 0)
	{
		setWriterError("injected_late_failure", "failure injected after completion write", TRUE);
	}
	flushOutput();
	FILE *output = s_output;
	s_output = nullptr;
	s_initialized = FALSE;
	if (fclose(output) != 0)
	{
		setWriterError("close_failed", "could not close telemetry output", TRUE);
	}
	if (s_outputFailed)
	{
		discardTemporaryOutput();
	}
	else
	{
		publishTemporaryOutput();
	}
}

void ReplayTelemetry::discard()
{
	// TheSuperHackers @feature Leex 20/08/2026 Discard reset and reconfiguration transactions that have no replay termination boundary. (#TBD)
	s_finishDeferred = FALSE;
	if (s_output != nullptr)
	{
		discardPendingOutput("could not close discarded telemetry output");
	}
}

void ReplayTelemetry::fail(const char *code, const char *message)
{
	if (isEnabled())
	{
		setWriterError(code, message, TRUE);
	}
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

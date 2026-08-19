#include "PreRTS.h"

#if defined(RTS_REPLAY_ANALYZER)

#include "Common/ReplayParseDump.h"

#include <cstdio>
#include <cstring>

namespace
{
	FILE *s_output = nullptr;
	AsciiString s_outputPath;

	void writeRaw(const char *text)
	{
		if (s_output != nullptr)
		{
			fputs(text, s_output);
		}
	}

	void writeHex(UnsignedInt value)
	{
		if (s_output != nullptr)
		{
			fprintf(s_output, "\"0x%08X\"", value);
		}
	}

	UnsignedInt realBits(Real value)
	{
		UnsignedInt bits = 0;
		memcpy(&bits, &value, sizeof(bits));
		return bits;
	}

	void writeJsonCodePoint(UnsignedInt codePoint)
	{
		if (codePoint == '"')
		{
			writeRaw("\\\"");
		}
		else if (codePoint == '\\')
		{
			writeRaw("\\\\");
		}
		else if (codePoint == '\b')
		{
			writeRaw("\\b");
		}
		else if (codePoint == '\f')
		{
			writeRaw("\\f");
		}
		else if (codePoint == '\n')
		{
			writeRaw("\\n");
		}
		else if (codePoint == '\r')
		{
			writeRaw("\\r");
		}
		else if (codePoint == '\t')
		{
			writeRaw("\\t");
		}
		else if (codePoint < 0x20)
		{
			if (s_output != nullptr)
			{
				fprintf(s_output, "\\u%04X", codePoint);
			}
		}
		else if (codePoint < 0x80)
		{
			if (s_output != nullptr)
			{
				fputc((int)codePoint, s_output);
			}
		}
		else if (codePoint < 0x800)
		{
			fputc(0xC0 | (codePoint >> 6), s_output);
			fputc(0x80 | (codePoint & 0x3F), s_output);
		}
		else if (codePoint < 0x10000)
		{
			fputc(0xE0 | (codePoint >> 12), s_output);
			fputc(0x80 | ((codePoint >> 6) & 0x3F), s_output);
			fputc(0x80 | (codePoint & 0x3F), s_output);
		}
		else
		{
			fputc(0xF0 | (codePoint >> 18), s_output);
			fputc(0x80 | ((codePoint >> 12) & 0x3F), s_output);
			fputc(0x80 | ((codePoint >> 6) & 0x3F), s_output);
			fputc(0x80 | (codePoint & 0x3F), s_output);
		}
	}

	void writeEscapedAscii(const AsciiString &value)
	{
		fputc('"', s_output);
		for (Int i = 0; i < value.getLength(); ++i)
		{
			writeJsonCodePoint((UnsignedByte)value.getCharAt(i));
		}
		fputc('"', s_output);
	}

	void writeEscapedUnicode(const UnicodeString &value)
	{
		fputc('"', s_output);
		for (Int i = 0; i < value.getLength(); ++i)
		{
			UnsignedInt codePoint = (UnsignedInt)value.getCharAt(i);
			if (codePoint >= 0xD800 && codePoint <= 0xDBFF && i + 1 < value.getLength())
			{
				const UnsignedInt low = (UnsignedInt)value.getCharAt(i + 1);
				if (low >= 0xDC00 && low <= 0xDFFF)
				{
					codePoint = 0x10000 + ((codePoint - 0xD800) << 10) + (low - 0xDC00);
					++i;
				}
			}
			if (codePoint >= 0xD800 && codePoint <= 0xDFFF)
			{
				codePoint = 0xFFFD;
			}
			writeJsonCodePoint(codePoint);
		}
		fputc('"', s_output);
	}

	const char *argumentTypeName(GameMessageArgumentDataType type)
	{
		switch (type)
		{
		case ARGUMENTDATATYPE_INTEGER: return "INTEGER";
		case ARGUMENTDATATYPE_REAL: return "REAL";
		case ARGUMENTDATATYPE_BOOLEAN: return "BOOLEAN";
		case ARGUMENTDATATYPE_OBJECTID: return "OBJECT_ID";
		case ARGUMENTDATATYPE_DRAWABLEID: return "DRAWABLE_ID";
		case ARGUMENTDATATYPE_TEAMID: return "TEAM_ID";
		case ARGUMENTDATATYPE_LOCATION: return "LOCATION";
		case ARGUMENTDATATYPE_PIXEL: return "PIXEL";
		case ARGUMENTDATATYPE_PIXELREGION: return "PIXEL_REGION";
		case ARGUMENTDATATYPE_TIMESTAMP: return "TIMESTAMP";
		case ARGUMENTDATATYPE_WIDECHAR: return "WIDE_CHAR";
		case ARGUMENTDATATYPE_UNKNOWN: return "UNKNOWN";
		default: return "UNRECOGNIZED";
		}
	}

	void writeRealDecimal(Real value)
	{
		fprintf(s_output, "%.9g", value);
	}

	void writeArgument(GameMessageArgumentDataType type, const GameMessageArgumentType &argument)
	{
		fprintf(s_output, "{\"type\":%d,\"type_name\":\"%s\",\"value\":", (Int)type, argumentTypeName(type));
		switch (type)
		{
		case ARGUMENTDATATYPE_INTEGER:
			fprintf(s_output, "%d,\"raw_scalar_bits\":", argument.integer);
			writeHex((UnsignedInt)argument.integer);
			break;
		case ARGUMENTDATATYPE_REAL:
			writeRealDecimal(argument.real);
			writeRaw(",\"raw_scalar_bits\":");
			writeHex(realBits(argument.real));
			break;
		case ARGUMENTDATATYPE_BOOLEAN:
			writeRaw(argument.boolean ? "true,\"raw_scalar_bits\":\"0x01\"" : "false,\"raw_scalar_bits\":\"0x00\"");
			break;
		case ARGUMENTDATATYPE_OBJECTID:
			fprintf(s_output, "%d,\"raw_scalar_bits\":", (Int)argument.objectID);
			writeHex((UnsignedInt)(Int)argument.objectID);
			break;
		case ARGUMENTDATATYPE_DRAWABLEID:
			fprintf(s_output, "%d,\"raw_scalar_bits\":", (Int)argument.drawableID);
			writeHex((UnsignedInt)(Int)argument.drawableID);
			break;
		case ARGUMENTDATATYPE_TEAMID:
			fprintf(s_output, "%u,\"raw_scalar_bits\":", argument.teamID);
			writeHex(argument.teamID);
			break;
		case ARGUMENTDATATYPE_LOCATION:
			writeRaw("{\"x\":");
			writeRealDecimal(argument.location.x);
			writeRaw(",\"y\":");
			writeRealDecimal(argument.location.y);
			writeRaw(",\"z\":");
			writeRealDecimal(argument.location.z);
			writeRaw("},\"raw_scalar_bits\":[");
			writeHex(realBits(argument.location.x));
			writeRaw(",");
			writeHex(realBits(argument.location.y));
			writeRaw(",");
			writeHex(realBits(argument.location.z));
			writeRaw("]");
			break;
		case ARGUMENTDATATYPE_PIXEL:
			fprintf(s_output, "{\"x\":%d,\"y\":%d},\"raw_scalar_bits\":[", argument.pixel.x, argument.pixel.y);
			writeHex((UnsignedInt)argument.pixel.x);
			writeRaw(",");
			writeHex((UnsignedInt)argument.pixel.y);
			writeRaw("]");
			break;
		case ARGUMENTDATATYPE_PIXELREGION:
			fprintf(s_output, "{\"lo\":{\"x\":%d,\"y\":%d},\"hi\":{\"x\":%d,\"y\":%d}},\"raw_scalar_bits\":[", argument.pixelRegion.lo.x, argument.pixelRegion.lo.y, argument.pixelRegion.hi.x, argument.pixelRegion.hi.y);
			writeHex((UnsignedInt)argument.pixelRegion.lo.x);
			writeRaw(",");
			writeHex((UnsignedInt)argument.pixelRegion.lo.y);
			writeRaw(",");
			writeHex((UnsignedInt)argument.pixelRegion.hi.x);
			writeRaw(",");
			writeHex((UnsignedInt)argument.pixelRegion.hi.y);
			writeRaw("]");
			break;
		case ARGUMENTDATATYPE_TIMESTAMP:
			fprintf(s_output, "%u,\"raw_scalar_bits\":", argument.timestamp);
			writeHex(argument.timestamp);
			break;
		case ARGUMENTDATATYPE_WIDECHAR:
			writeRaw("\"");
			writeJsonCodePoint((UnsignedInt)argument.wChar);
			writeRaw("\",\"raw_scalar_bits\":");
			fprintf(s_output, "\"0x%04X\"", (UnsignedInt)argument.wChar);
			break;
		default:
			writeRaw("null,\"raw_scalar_bits\":null");
			break;
		}
		writeRaw("}");
	}
}

void ReplayParseDump::setOutputPath(const AsciiString &path)
{
	if (s_output != nullptr)
	{
		fclose(s_output);
		s_output = nullptr;
	}
	s_outputPath = path;
}

Bool ReplayParseDump::isEnabled()
{
	return s_output != nullptr || s_outputPath.isNotEmpty();
}

Bool ReplayParseDump::beginReplay(const RecorderClass::ReplayHeader &header, Int endOffset)
{
	if (s_output != nullptr)
	{
		fclose(s_output);
		s_output = nullptr;
	}
	if (s_outputPath.isEmpty())
	{
		return FALSE;
	}

	s_output = fopen(s_outputPath.str(), "wb");
	if (s_output == nullptr)
	{
		return FALSE;
	}

	writeRaw("{\"record\":\"header\",\"filename\":");
	writeEscapedAscii(header.filename);
	fprintf(s_output, ",\"for_playback\":%s,\"start_time\":%lld,\"end_time\":%lld,\"frame_count\":%u,\"desync_game\":%s,\"quit_early\":%s,\"player_disconnects\":[", header.forPlayback ? "true" : "false", (long long)header.startTime, (long long)header.endTime, header.frameCount, header.desyncGame ? "true" : "false", header.quitEarly ? "true" : "false");
	for (Int i = 0; i < MAX_SLOTS; ++i)
	{
		if (i != 0)
		{
			writeRaw(",");
		}
		writeRaw(header.playerDiscons[i] ? "true" : "false");
	}
	writeRaw("],\"replay_name\":");
	writeEscapedUnicode(header.replayName);
	fprintf(s_output, ",\"system_time\":{\"year\":%u,\"month\":%u,\"day_of_week\":%u,\"day\":%u,\"hour\":%u,\"minute\":%u,\"second\":%u,\"milliseconds\":%u}", header.timeVal.wYear, header.timeVal.wMonth, header.timeVal.wDayOfWeek, header.timeVal.wDay, header.timeVal.wHour, header.timeVal.wMinute, header.timeVal.wSecond, header.timeVal.wMilliseconds);
	writeRaw(",\"version_string\":");
	writeEscapedUnicode(header.versionString);
	writeRaw(",\"version_time_string\":");
	writeEscapedUnicode(header.versionTimeString);
	fprintf(s_output, ",\"version_number\":%u,\"exe_crc\":%u,\"ini_crc\":%u,\"game_options\":", header.versionNumber, header.exeCRC, header.iniCRC);
	writeEscapedAscii(header.gameOptions);
	fprintf(s_output, ",\"local_player_index\":%d,\"header_end_offset\":%d}\n", header.localPlayerIndex, endOffset);
	writeMessageCatalog();
	return TRUE;
}

void ReplayParseDump::writeSetup(Int difficulty, Int originalGameMode, Int rankPoints, Int maxFPS, Int startOffset, Int endOffset)
{
	if (s_output != nullptr)
	{
		fprintf(s_output, "{\"record\":\"setup\",\"difficulty\":%d,\"original_game_mode\":%d,\"rank_points\":%d,\"max_fps\":%d,\"start_offset\":%d,\"end_offset\":%d}\n", difficulty, originalGameMode, rankPoints, maxFPS, startOffset, endOffset);
	}
}

void ReplayParseDump::writeCommand(Int frame, Int startOffset, Int endOffset, const GameMessage &message)
{
	if (s_output == nullptr)
	{
		return;
	}

	const GameMessage::Type type = message.getType();
	fprintf(s_output, "{\"record\":\"command\",\"frame\":%d,\"start_offset\":%d,\"end_offset\":%d,\"message_type\":%d,\"message_name\":", frame, startOffset, endOffset, (Int)type);
	AsciiString name = GameMessage::getCommandTypeAsString(type);
	writeEscapedAscii(name);
	fprintf(s_output, ",\"player_index\":%d,\"arguments\":[", message.getPlayerIndex());
	for (Int i = 0; i < (Int)message.getArgumentCount(); ++i)
	{
		if (i != 0)
		{
			writeRaw(",");
		}
		writeArgument(message.getArgumentDataType(i), *message.getArgument(i));
	}
	writeRaw("]}\n");
}

void ReplayParseDump::writeMessageCatalog()
{
	if (s_output == nullptr)
	{
		return;
	}

	writeRaw("{\"record\":\"message_catalog\",\"messages\":[");
	for (Int i = (Int)GameMessage::MSG_INVALID; i < (Int)GameMessage::MSG_COUNT; ++i)
	{
		if (i != (Int)GameMessage::MSG_INVALID)
		{
			writeRaw(",");
		}
		const GameMessage::Type type = (GameMessage::Type)i;
		fprintf(s_output, "{\"message_type\":%d,\"message_name\":", i);
		AsciiString name = GameMessage::getCommandTypeAsString(type);
		writeEscapedAscii(name);
		writeRaw("}");
	}
	writeRaw("]}\n");
}

void ReplayParseDump::finishReplay(Int endOffset, Bool complete)
{
	if (s_output != nullptr)
	{
		fprintf(s_output, "{\"record\":\"complete\",\"end_offset\":%d,\"complete\":%s}\n", endOffset, complete ? "true" : "false");
		fclose(s_output);
		s_output = nullptr;
	}
}

#endif // defined(RTS_REPLAY_ANALYZER)

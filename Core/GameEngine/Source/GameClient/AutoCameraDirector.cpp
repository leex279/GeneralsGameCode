/*
**	Command & Conquer Generals Zero Hour(tm)
**	Copyright 2026 TheSuperHackers
**
**	This program is free software: you can redistribute it and/or modify
**	it under the terms of the GNU General Public License as published by
**	the Free Software Foundation, either version 3 of the License, or
**	(at your option) any later version.
**
**	This program is distributed in the hope that it will be useful,
**	but WITHOUT ANY WARRANTY; without even the implied warranty of
**	MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
**	GNU General Public License for more details.
**
**	You should have received a copy of the GNU General Public License
**	along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/

#include "PreRTS.h"

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "GameClient/AutoCameraDirector.h"

#include "GameClient/View.h"
#include "GameLogic/GameLogic.h"

#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <set>
#include <string>

namespace
{
	const size_t EXPECTED_CAMERA_COLUMNS = 10;
	const Real CAMERA_COORDINATE_LIMIT = 10000000.0f;
	const Real MIN_CAMERA_ZOOM = 0.1f;
	const Real MAX_CAMERA_ZOOM = 10.0f;
	const Real MIN_CAMERA_PITCH = -89.0f;
	const Real MAX_CAMERA_PITCH = 0.0f;
	const Real MIN_CAMERA_YAW = -360.0f;
	const Real MAX_CAMERA_YAW = 360.0f;
	const size_t MAX_CAMERA_LINE_LENGTH = 1024;
	struct AutoCameraValidationCache
	{
		AsciiString m_script;
		std::vector<AutoCameraSegment> m_segments;
	};

	// TheSuperHackers @bugfix Leex 24/08/2026 Keep optional camera validation storage inert until command-line validation runs after engine memory initialization. (#TBD)
	AutoCameraValidationCache *s_validatedCameraCache = nullptr;

	class ScopedCameraFile
	{
	public:
		explicit ScopedCameraFile(FILE *file) : m_file(file) {}
		~ScopedCameraFile()
		{
			if (m_file != nullptr)
			{
				fclose(m_file);
			}
		}

		FILE *get() const { return m_file; }

	private:
		ScopedCameraFile(const ScopedCameraFile &);
		ScopedCameraFile &operator=(const ScopedCameraFile &);
		FILE *m_file;
	};

	void setError(AsciiString *error, const Char *message, UnsignedInt lineNumber = 0)
	{
		if (error == nullptr)
		{
			return;
		}
		if (lineNumber == 0)
		{
			*error = message;
		}
		else
		{
			error->format("line %u: %s", lineNumber, message);
		}
	}

	std::string trim(const std::string &value)
	{
		const std::string whitespace(" \t\r\n");
		const size_t first = value.find_first_not_of(whitespace);
		if (first == std::string::npos)
		{
			return std::string();
		}
		const size_t last = value.find_last_not_of(whitespace);
		return value.substr(first, last - first + 1);
	}

	Bool parseFrame(const std::string &text, UnsignedInt *value)
	{
		if (text.empty() || value == nullptr || text[0] == '-')
		{
			return FALSE;
		}
		Char *end = nullptr;
		errno = 0;
		const unsigned long parsed = std::strtoul(text.c_str(), &end, 10);
		if (end == text.c_str() || *end != '\0' || errno == ERANGE
			|| parsed > std::numeric_limits<UnsignedInt>::max())
		{
			return FALSE;
		}
		*value = static_cast<UnsignedInt>(parsed);
		return TRUE;
	}

	Bool parseFiniteReal(const std::string &text, Real minimum, Real maximum, Real *value)
	{
		if (text.empty() || value == nullptr)
		{
			return FALSE;
		}
		Char *end = nullptr;
		errno = 0;
		const double parsed = std::strtod(text.c_str(), &end);
		if (end == text.c_str() || *end != '\0' || errno == ERANGE || !std::isfinite(parsed)
			|| (parsed == 0.0 && std::signbit(parsed)) || parsed < minimum || parsed > maximum)
		{
			return FALSE;
		}
		*value = static_cast<Real>(parsed);
		return TRUE;
	}

	Bool isLowerHex(Char value)
	{
		return (value >= '0' && value <= '9') || (value >= 'a' && value <= 'f');
	}

	Bool isCanonicalSegmentId(const std::string &value)
	{
		if (value.length() != 36)
		{
			return FALSE;
		}
		for (size_t index = 0; index < value.length(); ++index)
		{
			if (index == 8 || index == 13 || index == 18 || index == 23)
			{
				if (value[index] != '-')
				{
					return FALSE;
				}
			}
			else if (!isLowerHex(value[index]))
			{
				return FALSE;
			}
		}
		return TRUE;
	}

	Bool splitCameraRow(const std::string &line, std::vector<std::string> *columns)
	{
		columns->clear();
		size_t start = 0;
		while (true)
		{
			const size_t comma = line.find(',', start);
			if (comma == std::string::npos)
			{
				columns->push_back(trim(line.substr(start)));
				break;
			}
			columns->push_back(trim(line.substr(start, comma - start)));
			start = comma + 1;
		}
		return columns->size() == EXPECTED_CAMERA_COLUMNS;
	}

	Bool parseCameraScript(
		const AsciiString &filename,
		std::vector<AutoCameraSegment> *parsedSegments,
		AsciiString *error)
	{
		parsedSegments->clear();
		if (filename.isEmpty())
		{
			setError(error, "camera script path is empty");
			return FALSE;
		}

		ScopedCameraFile file(fopen(filename.str(), "rb"));
		if (file.get() == nullptr)
		{
			setError(error, "camera script could not be opened");
			return FALSE;
		}

		std::set<std::string> segmentIds;
		UnsignedInt lineNumber = 0;
		Char lineBuffer[MAX_CAMERA_LINE_LENGTH + 3];
		while (fgets(lineBuffer, sizeof(lineBuffer), file.get()) != nullptr)
		{
			++lineNumber;
			std::string line(lineBuffer);
			if (!line.empty() && line[line.length() - 1] == '\n')
			{
				line.erase(line.length() - 1);
			}
			if (!line.empty() && line[line.length() - 1] == '\r')
			{
				line.erase(line.length() - 1);
			}
			if (line.length() > MAX_CAMERA_LINE_LENGTH)
			{
				setError(error, "camera row is too long", lineNumber);
				return FALSE;
			}
			std::vector<std::string> columns;
			if (!splitCameraRow(line, &columns))
			{
				setError(error, "camera row has an invalid field count", lineNumber);
				return FALSE;
			}

			AutoCameraSegment segment;
			if (!parseFrame(columns[0], &segment.m_startFrame)
				|| !parseFrame(columns[1], &segment.m_endFrame)
				|| segment.m_endFrame < segment.m_startFrame)
			{
				setError(error, "camera frame interval is invalid", lineNumber);
				return FALSE;
			}
			if (!parseFiniteReal(columns[2], -CAMERA_COORDINATE_LIMIT, CAMERA_COORDINATE_LIMIT, &segment.m_targetPos.x)
				|| !parseFiniteReal(columns[3], -CAMERA_COORDINATE_LIMIT, CAMERA_COORDINATE_LIMIT, &segment.m_targetPos.y)
				|| !parseFiniteReal(columns[4], -CAMERA_COORDINATE_LIMIT, CAMERA_COORDINATE_LIMIT, &segment.m_targetPos.z)
				|| !parseFiniteReal(columns[5], MIN_CAMERA_ZOOM, MAX_CAMERA_ZOOM, &segment.m_zoom)
				|| !parseFiniteReal(columns[6], MIN_CAMERA_PITCH, MAX_CAMERA_PITCH, &segment.m_pitch)
				|| !parseFiniteReal(columns[7], MIN_CAMERA_YAW, MAX_CAMERA_YAW, &segment.m_yaw))
			{
				setError(error, "camera row contains a non-finite or out-of-range value", lineNumber);
				return FALSE;
			}

			const std::string &transition = columns[8];
			if (transition == "cut")
			{
				segment.m_transition = AUTO_CAMERA_TRANSITION_CUT;
			}
			else if (transition == "ease")
			{
				// TheSuperHackers @info Leex 23/08/2026 Ease rows are baked transition windows; exporters split later holds into same-target rows. (#TBD)
				if (parsedSegments->empty())
				{
					setError(error, "ease transition cannot be the first camera row", lineNumber);
					return FALSE;
				}
				if (segment.m_endFrame == segment.m_startFrame)
				{
					setError(error, "ease transition must span at least two frames", lineNumber);
					return FALSE;
				}
				segment.m_transition = AUTO_CAMERA_TRANSITION_EASE;
			}
			else
			{
				setError(error, "camera transition must be cut or ease", lineNumber);
				return FALSE;
			}

			if (!isCanonicalSegmentId(columns[9]))
			{
				setError(error, "camera segment ID must be a canonical lowercase UUID", lineNumber);
				return FALSE;
			}
			if (!segmentIds.insert(columns[9]).second)
			{
				setError(error, "camera segment IDs must be unique", lineNumber);
				return FALSE;
			}
			segment.m_segmentId = columns[9].c_str();

			if (parsedSegments->empty())
			{
				if (segment.m_startFrame != 0)
				{
					setError(error, "start frame must be zero", lineNumber);
					return FALSE;
				}
			}
			else
			{
				const AutoCameraSegment &previous = parsedSegments->back();
				if (previous.m_endFrame == std::numeric_limits<UnsignedInt>::max()
					|| segment.m_startFrame != previous.m_endFrame + 1)
				{
					setError(error, "camera rows must be inclusive and gapless", lineNumber);
					return FALSE;
				}
			}
			parsedSegments->push_back(segment);
		}

		if (ferror(file.get()))
		{
			setError(error, "camera script could not be read completely");
			return FALSE;
		}
		if (parsedSegments->empty())
		{
			setError(error, "camera script contains no rows");
			return FALSE;
		}
		return TRUE;
	}

	Real smoothStep(Real value)
	{
		return value * value * (3.0f - 2.0f * value);
	}

	Real interpolate(Real from, Real to, Real amount)
	{
		return from + (to - from) * amount;
	}

	Real interpolateYaw(Real from, Real to, Real amount)
	{
		Real delta = to - from;
		while (delta > 180.0f)
		{
			delta -= 360.0f;
		}
		while (delta < -180.0f)
		{
			delta += 360.0f;
		}
		return from + delta * amount;
	}
}

// TheSuperHackers @feature Leex 23/08/2026 Drive replay presentation from a validated seek-safe frame script without changing GameLogic. (#TBD)
AutoCameraDirector *TheAutoCameraDirector = nullptr;

AutoCameraDirector::AutoCameraDirector() : m_enabled(FALSE)
{
}

AutoCameraDirector::~AutoCameraDirector()
{
	m_segments.clear();
}

void AutoCameraDirector::init()
{
	m_enabled = FALSE;
	if (s_validatedCameraCache == nullptr)
	{
		return;
	}
	AsciiString filename = s_validatedCameraCache->m_script;
	AsciiString error;
	if (!loadCameraScript(filename, &error))
	{
		fprintf(stderr, "Replay camera: %s\n", error.str());
		fflush(stderr);
		exit(1);
	}
	m_enabled = TRUE;
}

void AutoCameraDirector::reset()
{
	// The script is immutable and evaluation retains no cursor, so replay seeks and resets need no state mutation.
}

Bool AutoCameraDirector::validateCameraScript(const AsciiString &filename, AsciiString *error)
{
	std::vector<AutoCameraSegment> parsedSegments;
	if (!parseCameraScript(filename, &parsedSegments, error))
	{
		return FALSE;
	}
	AutoCameraValidationCache *cache = new AutoCameraValidationCache;
	cache->m_script = filename;
	cache->m_segments.swap(parsedSegments);
	delete s_validatedCameraCache;
	s_validatedCameraCache = cache;
	return TRUE;
}

Bool AutoCameraDirector::loadCameraScript(const AsciiString &filename, AsciiString *error)
{
	if (s_validatedCameraCache == nullptr || s_validatedCameraCache->m_segments.empty()
		|| strcmp(filename.str(), s_validatedCameraCache->m_script.str()) != 0)
	{
		setError(error, "camera script does not match the validated startup snapshot");
		return FALSE;
	}
	m_segments = s_validatedCameraCache->m_segments;
	// TheSuperHackers @bugfix Leex 24/08/2026 Consume the immutable startup snapshot after GameClient owns its copy so it cannot be reopened or outlive client initialization. (#TBD)
	delete s_validatedCameraCache;
	s_validatedCameraCache = nullptr;
	return TRUE;
}

Bool AutoCameraDirector::evaluateCameraAtFrame(UnsignedInt frame, AutoCameraSegment *camera) const
{
	if (camera == nullptr || m_segments.empty())
	{
		return FALSE;
	}

	size_t index = 0;
	while (index + 1 < m_segments.size() && frame > m_segments[index].m_endFrame)
	{
		++index;
	}
	const AutoCameraSegment &current = m_segments[index];
	*camera = current;
	if (index == 0 || current.m_transition == AUTO_CAMERA_TRANSITION_CUT || frame >= current.m_endFrame)
	{
		return TRUE;
	}

	const AutoCameraSegment &previous = m_segments[index - 1];
	const UnsignedInt elapsed = frame > current.m_startFrame ? frame - current.m_startFrame : 0;
	const UnsignedInt duration = current.m_endFrame - current.m_startFrame;
	const Real amount = duration == 0 ? 1.0f : smoothStep(static_cast<Real>(elapsed) / static_cast<Real>(duration));
	camera->m_targetPos.x = interpolate(previous.m_targetPos.x, current.m_targetPos.x, amount);
	camera->m_targetPos.y = interpolate(previous.m_targetPos.y, current.m_targetPos.y, amount);
	camera->m_targetPos.z = interpolate(previous.m_targetPos.z, current.m_targetPos.z, amount);
	camera->m_zoom = interpolate(previous.m_zoom, current.m_zoom, amount);
	camera->m_pitch = interpolate(previous.m_pitch, current.m_pitch, amount);
	camera->m_yaw = interpolateYaw(previous.m_yaw, current.m_yaw, amount);
	return TRUE;
}

void AutoCameraDirector::update()
{
	if (!m_enabled || TheGameLogic == nullptr || TheTacticalView == nullptr)
	{
		return;
	}

	AutoCameraSegment camera;
	if (evaluateCameraAtFrame(TheGameLogic->getFrame(), &camera))
	{
		TheTacticalView->lookAt(&camera.m_targetPos);
		TheTacticalView->setZoom(camera.m_zoom);
		TheTacticalView->setPitch(DEG_TO_RADF(-camera.m_pitch));
		TheTacticalView->setAngle(DEG_TO_RADF(camera.m_yaw));
	}
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

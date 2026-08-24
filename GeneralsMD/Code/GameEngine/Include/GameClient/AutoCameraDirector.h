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

#pragma once

#include "Common/AsciiString.h"
#include "Common/GameType.h"
#include "Common/SubsystemInterface.h"

#include <vector>

enum AutoCameraTransition
{
	AUTO_CAMERA_TRANSITION_CUT,
	AUTO_CAMERA_TRANSITION_EASE,
};

// TheSuperHackers @feature Leex 23/08/2026 Keep validated replay direction in presentation-only integer-frame records. (#TBD)
struct AutoCameraSegment
{
	UnsignedInt m_startFrame;
	UnsignedInt m_endFrame;
	Coord3D m_targetPos;
	Real m_zoom;
	Real m_pitch;
	Real m_yaw;
	AutoCameraTransition m_transition;
	AsciiString m_segmentId;
};

class AutoCameraDirector : public SubsystemInterface
{
public:
	AutoCameraDirector();
	virtual ~AutoCameraDirector();

	virtual void init() override;
	virtual void reset() override;
	virtual void update() override;

	static Bool validateCameraScript(const AsciiString &filename, AsciiString *error);
	Bool loadCameraScript(const AsciiString &filename, AsciiString *error);
	Bool evaluateCameraAtFrame(UnsignedInt frame, AutoCameraSegment *camera) const;
	Bool isEnabled() const { return m_enabled; }

private:
	Bool m_enabled;
	std::vector<AutoCameraSegment> m_segments;
};

extern AutoCameraDirector *TheAutoCameraDirector;

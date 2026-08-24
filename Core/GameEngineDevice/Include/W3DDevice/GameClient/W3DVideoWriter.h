/*
**	Command & Conquer Generals Zero Hour(tm)
**	Copyright 2026 TheSuperHackers
**
**	This program is free software: you can redistribute it and/or modify
**	it under the terms of the GNU General Public License as published by
**	the Free Software Foundation, either version 3 of the License, or
**	(at your option) any later version.
*/

#pragma once

#include <windows.h>

#include <limits>
#include <string>
#include <vector>

struct IDirect3DDevice8;
struct IDirect3DSurface8;

enum W3DVideoCaptureFailure
{
	W3D_VIDEO_CAPTURE_OK = 0,
	W3D_VIDEO_CAPTURE_INVALID_CONFIGURATION,
	W3D_VIDEO_CAPTURE_UNSUPPORTED_SURFACE_FORMAT,
	W3D_VIDEO_CAPTURE_BACKBUFFER_QUERY_FAILED,
	W3D_VIDEO_CAPTURE_STAGING_SURFACE_CREATE_FAILED,
	W3D_VIDEO_CAPTURE_SURFACE_COPY_FAILED,
	W3D_VIDEO_CAPTURE_SURFACE_LOCK_FAILED,
	W3D_VIDEO_CAPTURE_SURFACE_UNLOCK_FAILED,
	W3D_VIDEO_CAPTURE_INVALID_PITCH,
	W3D_VIDEO_CAPTURE_PIXEL_CONVERSION_FAILED,
	W3D_VIDEO_CAPTURE_PIPE_CREATE_FAILED,
	W3D_VIDEO_CAPTURE_PROCESS_LAUNCH_FAILED,
	W3D_VIDEO_CAPTURE_PIPE_WRITE_FAILED,
	W3D_VIDEO_CAPTURE_PIPE_CLOSE_FAILED,
	W3D_VIDEO_CAPTURE_PROCESS_WAIT_FAILED,
	W3D_VIDEO_CAPTURE_PROCESS_EXIT_FAILED,
	W3D_VIDEO_CAPTURE_OUTPUT_MISSING,
	W3D_VIDEO_CAPTURE_LOGIC_FRAME_REGRESSED,
	W3D_VIDEO_CAPTURE_LOGIC_FRAME_GAP,
	W3D_VIDEO_CAPTURE_SIDECAR_WRITE_FAILED,
};

namespace W3DVideoCaptureContract
{
	// TheSuperHackers @feature Leex 23/08/2026 Encode one Windows argv item using the CommandLineToArgvW backslash rules. (#TBD)
	inline std::wstring quoteWindowsArgument(const std::wstring &argument)
	{
		if (!argument.empty() && argument.find_first_of(L" \t\"") == std::wstring::npos)
		{
			return argument;
		}

		std::wstring encoded(1, L'"');
		size_t backslashes = 0;
		for (size_t index = 0; index < argument.size(); ++index)
		{
			const wchar_t value = argument[index];
			if (value == L'\\')
			{
				++backslashes;
				continue;
			}
			if (value == L'"')
			{
				encoded.append(backslashes * 2 + 1, L'\\');
				encoded.push_back(L'"');
				backslashes = 0;
				continue;
			}
			encoded.append(backslashes, L'\\');
			backslashes = 0;
			encoded.push_back(value);
		}
		encoded.append(backslashes * 2, L'\\');
		encoded.push_back(L'"');
		return encoded;
	}

	// TheSuperHackers @feature Leex 23/08/2026 Convert only visible BGRA pixels so arbitrary positive GPU row pitch never leaks padding. (#TBD)
	inline bool convertBgraToRgb24(const unsigned char *source, int pitch, unsigned int width, unsigned int height,
		std::vector<unsigned char> &destination, W3DVideoCaptureFailure &failure)
	{
		const size_t maxSize = (std::numeric_limits<size_t>::max)();
		if (source == nullptr || pitch <= 0 || width == 0 || height == 0
			|| static_cast<size_t>(width) > maxSize / 4)
		{
			failure = W3D_VIDEO_CAPTURE_INVALID_PITCH;
			return false;
		}
		const size_t visibleSourceBytes = static_cast<size_t>(width) * 4;
		if (static_cast<size_t>(pitch) < visibleSourceBytes
			|| static_cast<size_t>(height - 1)
				> (maxSize - visibleSourceBytes) / static_cast<size_t>(pitch))
		{
			failure = W3D_VIDEO_CAPTURE_INVALID_PITCH;
			return false;
		}
		if (static_cast<size_t>(width) > maxSize / 3
			|| static_cast<size_t>(height) > maxSize / (static_cast<size_t>(width) * 3))
		{
			failure = W3D_VIDEO_CAPTURE_PIXEL_CONVERSION_FAILED;
			return false;
		}

		try
		{
			destination.resize(static_cast<size_t>(width) * height * 3);
		}
		catch (...)
		{
			failure = W3D_VIDEO_CAPTURE_PIXEL_CONVERSION_FAILED;
			return false;
		}
		for (unsigned int row = 0; row < height; ++row)
		{
			const unsigned char *sourceRow = source + static_cast<size_t>(row) * pitch;
			unsigned char *destinationRow = &destination[static_cast<size_t>(row) * width * 3];
			for (unsigned int column = 0; column < width; ++column)
			{
				const unsigned char *pixel = sourceRow + static_cast<size_t>(column) * 4;
				destinationRow[column * 3] = pixel[2];
				destinationRow[column * 3 + 1] = pixel[1];
				destinationRow[column * 3 + 2] = pixel[0];
			}
		}
		failure = W3D_VIDEO_CAPTURE_OK;
		return true;
	}

	// TheSuperHackers @feature Leex 23/08/2026 Express 60 FPS as two presentations of one 30 Hz simulation image. (#TBD)
	inline int presentationCopiesForFps(int fps)
	{
		return fps == 30 ? 1 : (fps == 60 ? 2 : 0);
	}
}

class W3DVideoWriter
{
public:
	W3DVideoWriter(const char *outputPath, int requestedWidth, int requestedHeight, int fps);
	~W3DVideoWriter();

	bool captureFrame(IDirect3DDevice8 *device, unsigned int logicFrame);
	void close();
	bool hasFailed() const { return m_failure != W3D_VIDEO_CAPTURE_OK; }

private:
	W3DVideoWriter(const W3DVideoWriter &);
	W3DVideoWriter &operator=(const W3DVideoWriter &);

	bool open(IDirect3DDevice8 *device, unsigned int width, unsigned int height, DWORD format);
	bool writeAll(const unsigned char *data, size_t byteCount);
	bool finishProcess();
	void fail(W3DVideoCaptureFailure failure, DWORD detail);
	void stopFailedProcess();
	void releaseSurface();
	void writeSidecar();

	std::wstring m_outputPath;
	std::wstring m_sidecarPath;
	int m_requestedWidth;
	int m_requestedHeight;
	int m_fps;
	unsigned int m_actualWidth;
	unsigned int m_actualHeight;
	DWORD m_surfaceFormat;
	int m_surfacePitch;
	LONG m_lastLogicFrame;
	unsigned int m_logicFrames;
	unsigned int m_presentationFrames;
	W3DVideoCaptureFailure m_failure;
	DWORD m_failureDetail;
	DWORD m_processExitCode;
	HANDLE m_pipeWrite;
	HANDLE m_process;
	IDirect3DSurface8 *m_stagingSurface;
	bool m_opened;
	bool m_closed;
	bool m_sidecarWritten;
	std::vector<unsigned char> m_rgb24;
};

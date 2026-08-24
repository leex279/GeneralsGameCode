/*
**	Command & Conquer Generals Zero Hour(tm)
**	Copyright 2026 TheSuperHackers
**
**	This program is free software: you can redistribute it and/or modify
**	it under the terms of the GNU General Public License as published by
**	the Free Software Foundation, either version 3 of the License, or
**	(at your option) any later version.
*/

#include "W3DDevice/GameClient/W3DVideoWriter.h"

#include <d3d8.h>

#include <cstdio>
#include <sstream>

namespace
{
	const DWORD FFmpegShutdownTimeoutMilliseconds = 30000;

	std::wstring ansiToWide(const char *value)
	{
		if (value == nullptr || *value == '\0')
		{
			return std::wstring();
		}
		const int length = MultiByteToWideChar(CP_ACP, MB_ERR_INVALID_CHARS, value, -1, nullptr, 0);
		if (length <= 1)
		{
			return std::wstring();
		}
		std::vector<wchar_t> buffer(static_cast<size_t>(length));
		if (MultiByteToWideChar(CP_ACP, MB_ERR_INVALID_CHARS, value, -1, &buffer[0], length) != length)
		{
			return std::wstring();
		}
		return std::wstring(&buffer[0]);
	}

	const char *failureName(W3DVideoCaptureFailure failure)
	{
		switch (failure)
		{
			case W3D_VIDEO_CAPTURE_OK: return "ok";
			case W3D_VIDEO_CAPTURE_INVALID_CONFIGURATION: return "invalid_configuration";
			case W3D_VIDEO_CAPTURE_UNSUPPORTED_SURFACE_FORMAT: return "unsupported_surface_format";
			case W3D_VIDEO_CAPTURE_BACKBUFFER_QUERY_FAILED: return "backbuffer_query_failed";
			case W3D_VIDEO_CAPTURE_STAGING_SURFACE_CREATE_FAILED: return "staging_surface_create_failed";
			case W3D_VIDEO_CAPTURE_SURFACE_COPY_FAILED: return "surface_copy_failed";
			case W3D_VIDEO_CAPTURE_SURFACE_LOCK_FAILED: return "surface_lock_failed";
			case W3D_VIDEO_CAPTURE_SURFACE_UNLOCK_FAILED: return "surface_unlock_failed";
			case W3D_VIDEO_CAPTURE_INVALID_PITCH: return "invalid_pitch";
			case W3D_VIDEO_CAPTURE_PIXEL_CONVERSION_FAILED: return "pixel_conversion_failed";
			case W3D_VIDEO_CAPTURE_PIPE_CREATE_FAILED: return "pipe_create_failed";
			case W3D_VIDEO_CAPTURE_PROCESS_LAUNCH_FAILED: return "process_launch_failed";
			case W3D_VIDEO_CAPTURE_PIPE_WRITE_FAILED: return "pipe_write_failed";
			case W3D_VIDEO_CAPTURE_PIPE_CLOSE_FAILED: return "pipe_close_failed";
			case W3D_VIDEO_CAPTURE_PROCESS_WAIT_FAILED: return "process_wait_failed";
			case W3D_VIDEO_CAPTURE_PROCESS_EXIT_FAILED: return "process_exit_failed";
			case W3D_VIDEO_CAPTURE_OUTPUT_MISSING: return "output_missing";
			case W3D_VIDEO_CAPTURE_LOGIC_FRAME_REGRESSED: return "logic_frame_regressed";
			case W3D_VIDEO_CAPTURE_LOGIC_FRAME_GAP: return "logic_frame_gap";
			case W3D_VIDEO_CAPTURE_SIDECAR_WRITE_FAILED: return "sidecar_write_failed";
		}
		return "unknown";
	}

	bool fileHasBytes(const std::wstring &path)
	{
		WIN32_FILE_ATTRIBUTE_DATA attributes;
		if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &attributes)
			|| (attributes.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
		{
			return false;
		}
		return attributes.nFileSizeHigh != 0 || attributes.nFileSizeLow != 0;
	}

	bool writeHandleAll(HANDLE handle, const char *data, size_t byteCount)
	{
		size_t offset = 0;
		while (offset < byteCount)
		{
			const size_t remaining = byteCount - offset;
			const DWORD requested = remaining > MAXDWORD ? MAXDWORD : static_cast<DWORD>(remaining);
			DWORD bytesWritten = 0;
			if (!WriteFile(handle, data + offset, requested, &bytesWritten, nullptr) || bytesWritten == 0)
			{
				return false;
			}
			offset += bytesWritten;
		}
		return true;
	}
}

// TheSuperHackers @feature Leex 23/08/2026 Own the D3D staging surface, restricted FFmpeg child, and typed capture result as one renderer-only lifetime. (#TBD)
W3DVideoWriter::W3DVideoWriter(const char *outputPath, int requestedWidth, int requestedHeight, int fps) :
	m_outputPath(ansiToWide(outputPath)),
	m_sidecarPath(m_outputPath + L".capture-result.json"),
	m_requestedWidth(requestedWidth),
	m_requestedHeight(requestedHeight),
	m_fps(fps),
	m_actualWidth(0),
	m_actualHeight(0),
	m_surfaceFormat(0),
	m_surfacePitch(0),
	m_lastLogicFrame(-1),
	m_logicFrames(0),
	m_presentationFrames(0),
	m_failure(W3D_VIDEO_CAPTURE_OK),
	m_failureDetail(ERROR_SUCCESS),
	m_processExitCode(STILL_ACTIVE),
	m_pipeWrite(INVALID_HANDLE_VALUE),
	m_process(INVALID_HANDLE_VALUE),
	m_stagingSurface(nullptr),
	m_opened(false),
	m_closed(false),
	m_sidecarWritten(false)
{
	if (m_outputPath.empty() || requestedWidth <= 0 || requestedHeight <= 0
		|| W3DVideoCaptureContract::presentationCopiesForFps(fps) == 0)
	{
		m_failure = W3D_VIDEO_CAPTURE_INVALID_CONFIGURATION;
		m_failureDetail = ERROR_INVALID_PARAMETER;
	}
}

W3DVideoWriter::~W3DVideoWriter()
{
	close();
	releaseSurface();
}

bool W3DVideoWriter::open(IDirect3DDevice8 *device, unsigned int width, unsigned int height, DWORD format)
{
	if (m_opened)
	{
		if (width != m_actualWidth || height != m_actualHeight || format != m_surfaceFormat)
		{
			fail(W3D_VIDEO_CAPTURE_INVALID_CONFIGURATION, ERROR_INVALID_DATA);
			return false;
		}
		return true;
	}
	if (m_failure != W3D_VIDEO_CAPTURE_OK || device == nullptr)
	{
		fail(W3D_VIDEO_CAPTURE_INVALID_CONFIGURATION, ERROR_INVALID_PARAMETER);
		return false;
	}
	if (format != D3DFMT_X8R8G8B8 && format != D3DFMT_A8R8G8B8)
	{
		fail(W3D_VIDEO_CAPTURE_UNSUPPORTED_SURFACE_FORMAT, format);
		return false;
	}

	HRESULT result = device->CreateImageSurface(width, height, static_cast<D3DFORMAT>(format), &m_stagingSurface);
	if (FAILED(result) || m_stagingSurface == nullptr)
	{
		fail(W3D_VIDEO_CAPTURE_STAGING_SURFACE_CREATE_FAILED, static_cast<DWORD>(result));
		return false;
	}

	wchar_t ffmpegBuffer[32768];
	const DWORD ffmpegLength = SearchPathW(nullptr, L"ffmpeg.exe", nullptr,
		static_cast<DWORD>(sizeof(ffmpegBuffer) / sizeof(ffmpegBuffer[0])), ffmpegBuffer, nullptr);
	if (ffmpegLength == 0 || ffmpegLength >= sizeof(ffmpegBuffer) / sizeof(ffmpegBuffer[0]))
	{
		fail(W3D_VIDEO_CAPTURE_PROCESS_LAUNCH_FAILED, GetLastError());
		return false;
	}
	const std::wstring resolvedFfmpegPath(ffmpegBuffer, ffmpegLength);

	SECURITY_ATTRIBUTES security;
	ZeroMemory(&security, sizeof(security));
	security.nLength = sizeof(security);
	security.bInheritHandle = TRUE;
	HANDLE pipeRead = INVALID_HANDLE_VALUE;
	if (!CreatePipe(&pipeRead, &m_pipeWrite, &security, 0)
		|| !SetHandleInformation(m_pipeWrite, HANDLE_FLAG_INHERIT, 0))
	{
		const DWORD detail = GetLastError();
		if (pipeRead != INVALID_HANDLE_VALUE) CloseHandle(pipeRead);
		if (m_pipeWrite != INVALID_HANDLE_VALUE) CloseHandle(m_pipeWrite);
		m_pipeWrite = INVALID_HANDLE_VALUE;
		fail(W3D_VIDEO_CAPTURE_PIPE_CREATE_FAILED, detail);
		return false;
	}
	HANDLE nullOutput = CreateFileW(L"NUL", GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &security,
		OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
	if (nullOutput == INVALID_HANDLE_VALUE)
	{
		const DWORD detail = GetLastError();
		CloseHandle(pipeRead);
		CloseHandle(m_pipeWrite);
		m_pipeWrite = INVALID_HANDLE_VALUE;
		fail(W3D_VIDEO_CAPTURE_PIPE_CREATE_FAILED, detail);
		return false;
	}

	std::wostringstream inputSize;
	inputSize << width << L"x" << height;
	std::wostringstream scale;
	scale << L"scale=" << m_requestedWidth << L":" << m_requestedHeight << L":flags=bicubic";
	std::wostringstream fps;
	fps << m_fps;
	std::vector<std::wstring> arguments;
	arguments.push_back(L"-hide_banner");
	arguments.push_back(L"-loglevel");
	arguments.push_back(L"error");
	arguments.push_back(L"-nostdin");
	arguments.push_back(L"-f");
	arguments.push_back(L"rawvideo");
	arguments.push_back(L"-pixel_format");
	arguments.push_back(L"rgb24");
	arguments.push_back(L"-video_size");
	arguments.push_back(inputSize.str());
	arguments.push_back(L"-framerate");
	arguments.push_back(fps.str());
	arguments.push_back(L"-i");
	arguments.push_back(L"pipe:0");
	arguments.push_back(L"-an");
	arguments.push_back(L"-vf");
	arguments.push_back(scale.str());
	arguments.push_back(L"-c:v");
	arguments.push_back(L"libx264");
	arguments.push_back(L"-pix_fmt");
	arguments.push_back(L"yuv420p");
	arguments.push_back(L"-fps_mode");
	arguments.push_back(L"cfr");
	arguments.push_back(L"-n");
	arguments.push_back(m_outputPath);

	std::wstring commandLine = W3DVideoCaptureContract::quoteWindowsArgument(resolvedFfmpegPath);
	for (size_t index = 0; index + 1 < arguments.size(); ++index)
	{
		commandLine += L" ";
		commandLine += W3DVideoCaptureContract::quoteWindowsArgument(arguments[index]);
	}
	commandLine += L" ";
	commandLine += W3DVideoCaptureContract::quoteWindowsArgument(m_outputPath);
	std::vector<wchar_t> mutableCommandLine(commandLine.begin(), commandLine.end());
	mutableCommandLine.push_back(L'\0');

	SIZE_T attributeBytes = 0;
	InitializeProcThreadAttributeList(nullptr, 1, 0, &attributeBytes);
	LPPROC_THREAD_ATTRIBUTE_LIST attributes = static_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(
		HeapAlloc(GetProcessHeap(), 0, attributeBytes));
	HANDLE inheritedHandles[] = { pipeRead, nullOutput };
	bool attributesReady = attributes != nullptr
		&& InitializeProcThreadAttributeList(attributes, 1, 0, &attributeBytes)
		&& UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
			inheritedHandles, sizeof(inheritedHandles), nullptr, nullptr);
	if (!attributesReady)
	{
		const DWORD detail = GetLastError();
		if (attributes != nullptr) HeapFree(GetProcessHeap(), 0, attributes);
		CloseHandle(pipeRead);
		CloseHandle(nullOutput);
		CloseHandle(m_pipeWrite);
		m_pipeWrite = INVALID_HANDLE_VALUE;
		fail(W3D_VIDEO_CAPTURE_PROCESS_LAUNCH_FAILED, detail);
		return false;
	}

	STARTUPINFOEXW startup;
	ZeroMemory(&startup, sizeof(startup));
	startup.StartupInfo.cb = sizeof(startup);
	startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
	startup.StartupInfo.hStdInput = pipeRead;
	startup.StartupInfo.hStdOutput = nullOutput;
	startup.StartupInfo.hStdError = nullOutput;
	startup.lpAttributeList = attributes;
	PROCESS_INFORMATION processInformation;
	ZeroMemory(&processInformation, sizeof(processInformation));
	const BOOL created = CreateProcessW(resolvedFfmpegPath.c_str(), &mutableCommandLine[0], nullptr, nullptr, TRUE,
		EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW, nullptr, nullptr, &startup.StartupInfo, &processInformation);
	const DWORD createDetail = created ? ERROR_SUCCESS : GetLastError();
	DeleteProcThreadAttributeList(attributes);
	HeapFree(GetProcessHeap(), 0, attributes);
	CloseHandle(pipeRead);
	CloseHandle(nullOutput);
	if (!created)
	{
		CloseHandle(m_pipeWrite);
		m_pipeWrite = INVALID_HANDLE_VALUE;
		fail(W3D_VIDEO_CAPTURE_PROCESS_LAUNCH_FAILED, createDetail);
		return false;
	}
	CloseHandle(processInformation.hThread);
	m_process = processInformation.hProcess;
	m_actualWidth = width;
	m_actualHeight = height;
	m_surfaceFormat = format;
	m_opened = true;
	return true;
}

bool W3DVideoWriter::captureFrame(IDirect3DDevice8 *device, unsigned int logicFrame)
{
	if (m_closed || m_failure != W3D_VIDEO_CAPTURE_OK)
	{
		return false;
	}
	if (m_lastLogicFrame >= 0)
	{
		if (logicFrame == static_cast<unsigned int>(m_lastLogicFrame)) return true;
		if (logicFrame < static_cast<unsigned int>(m_lastLogicFrame))
		{
			fail(W3D_VIDEO_CAPTURE_LOGIC_FRAME_REGRESSED, logicFrame);
			return false;
		}
		if (logicFrame != static_cast<unsigned int>(m_lastLogicFrame + 1))
		{
			fail(W3D_VIDEO_CAPTURE_LOGIC_FRAME_GAP, logicFrame);
			return false;
		}
	}

	IDirect3DSurface8 *backBuffer = nullptr;
	HRESULT result = device == nullptr ? E_POINTER
		: device->GetBackBuffer(0, D3DBACKBUFFER_TYPE_MONO, &backBuffer);
	D3DSURFACE_DESC description;
	ZeroMemory(&description, sizeof(description));
	if (FAILED(result) || backBuffer == nullptr)
	{
		if (backBuffer != nullptr) backBuffer->Release();
		fail(W3D_VIDEO_CAPTURE_BACKBUFFER_QUERY_FAILED, static_cast<DWORD>(result));
		return false;
	}
	const HRESULT descriptionResult = backBuffer->GetDesc(&description);
	if (FAILED(descriptionResult))
	{
		backBuffer->Release();
		fail(W3D_VIDEO_CAPTURE_BACKBUFFER_QUERY_FAILED, static_cast<DWORD>(descriptionResult));
		return false;
	}
	if (!open(device, description.Width, description.Height, description.Format))
	{
		backBuffer->Release();
		return false;
	}
	result = device->CopyRects(backBuffer, nullptr, 0, m_stagingSurface, nullptr);
	backBuffer->Release();
	if (FAILED(result))
	{
		fail(W3D_VIDEO_CAPTURE_SURFACE_COPY_FAILED, static_cast<DWORD>(result));
		return false;
	}

	D3DLOCKED_RECT locked;
	ZeroMemory(&locked, sizeof(locked));
	result = m_stagingSurface->LockRect(&locked, nullptr, D3DLOCK_READONLY);
	if (FAILED(result) || locked.pBits == nullptr)
	{
		fail(W3D_VIDEO_CAPTURE_SURFACE_LOCK_FAILED, static_cast<DWORD>(result));
		return false;
	}
	m_surfacePitch = locked.Pitch;
	W3DVideoCaptureFailure conversionFailure = W3D_VIDEO_CAPTURE_OK;
	const bool converted = W3DVideoCaptureContract::convertBgraToRgb24(
		static_cast<const unsigned char *>(locked.pBits), locked.Pitch,
		description.Width, description.Height, m_rgb24, conversionFailure);
	result = m_stagingSurface->UnlockRect();
	if (FAILED(result))
	{
		fail(W3D_VIDEO_CAPTURE_SURFACE_UNLOCK_FAILED, static_cast<DWORD>(result));
		return false;
	}
	if (!converted)
	{
		fail(conversionFailure, locked.Pitch);
		return false;
	}

	const int presentationCopies = W3DVideoCaptureContract::presentationCopiesForFps(m_fps);
	for (int copy = 0; copy < presentationCopies; ++copy)
	{
		if (!writeAll(&m_rgb24[0], m_rgb24.size())) return false;
		++m_presentationFrames;
	}
	m_lastLogicFrame = static_cast<LONG>(logicFrame);
	++m_logicFrames;
	return true;
}

bool W3DVideoWriter::writeAll(const unsigned char *data, size_t byteCount)
{
	if (m_pipeWrite == INVALID_HANDLE_VALUE)
	{
		fail(W3D_VIDEO_CAPTURE_PIPE_WRITE_FAILED, ERROR_INVALID_HANDLE);
		return false;
	}
	size_t offset = 0;
	while (offset < byteCount)
	{
		const size_t remaining = byteCount - offset;
		const DWORD requested = remaining > MAXDWORD ? MAXDWORD : static_cast<DWORD>(remaining);
		DWORD bytesWritten = 0;
		if (!WriteFile(m_pipeWrite, data + offset, requested, &bytesWritten, nullptr) || bytesWritten == 0)
		{
			fail(W3D_VIDEO_CAPTURE_PIPE_WRITE_FAILED, GetLastError());
			return false;
		}
		offset += bytesWritten;
	}
	return true;
}

bool W3DVideoWriter::finishProcess()
{
	if (!m_opened) return m_failure == W3D_VIDEO_CAPTURE_OK;
	if (m_pipeWrite != INVALID_HANDLE_VALUE)
	{
		if (!CloseHandle(m_pipeWrite))
		{
			m_pipeWrite = INVALID_HANDLE_VALUE;
			m_failure = W3D_VIDEO_CAPTURE_PIPE_CLOSE_FAILED;
			m_failureDetail = GetLastError();
			stopFailedProcess();
			return false;
		}
		m_pipeWrite = INVALID_HANDLE_VALUE;
	}
	const DWORD waitResult = WaitForSingleObject(m_process, FFmpegShutdownTimeoutMilliseconds);
	if (waitResult != WAIT_OBJECT_0)
	{
		m_failure = W3D_VIDEO_CAPTURE_PROCESS_WAIT_FAILED;
		m_failureDetail = waitResult == WAIT_FAILED ? GetLastError() : WAIT_TIMEOUT;
		stopFailedProcess();
		return false;
	}
	if (!GetExitCodeProcess(m_process, &m_processExitCode))
	{
		m_failure = W3D_VIDEO_CAPTURE_PROCESS_WAIT_FAILED;
		m_failureDetail = GetLastError();
		CloseHandle(m_process);
		m_process = INVALID_HANDLE_VALUE;
		return false;
	}
	CloseHandle(m_process);
	m_process = INVALID_HANDLE_VALUE;
	if (m_processExitCode != 0)
	{
		m_failure = W3D_VIDEO_CAPTURE_PROCESS_EXIT_FAILED;
		m_failureDetail = m_processExitCode;
		return false;
	}
	if (!fileHasBytes(m_outputPath))
	{
		m_failure = W3D_VIDEO_CAPTURE_OUTPUT_MISSING;
		m_failureDetail = GetLastError();
		return false;
	}
	return true;
}

void W3DVideoWriter::close()
{
	if (m_closed) return;
	if (!m_opened || m_presentationFrames == 0)
	{
		m_failure = W3D_VIDEO_CAPTURE_OUTPUT_MISSING;
		m_failureDetail = ERROR_NO_DATA;
	}
	if (m_failure == W3D_VIDEO_CAPTURE_OK) finishProcess();
	else stopFailedProcess();
	m_closed = true;
	writeSidecar();
}

void W3DVideoWriter::fail(W3DVideoCaptureFailure failure, DWORD detail)
{
	if (m_failure == W3D_VIDEO_CAPTURE_OK)
	{
		m_failure = failure;
		m_failureDetail = detail;
	}
	stopFailedProcess();
	m_closed = true;
	writeSidecar();
}

void W3DVideoWriter::stopFailedProcess()
{
	if (m_pipeWrite != INVALID_HANDLE_VALUE)
	{
		CloseHandle(m_pipeWrite);
		m_pipeWrite = INVALID_HANDLE_VALUE;
	}
	if (m_process != INVALID_HANDLE_VALUE)
	{
		const DWORD waitResult = WaitForSingleObject(m_process, 5000);
		if (waitResult != WAIT_OBJECT_0)
		{
			TerminateProcess(m_process, 1);
			WaitForSingleObject(m_process, 5000);
		}
		GetExitCodeProcess(m_process, &m_processExitCode);
		CloseHandle(m_process);
		m_process = INVALID_HANDLE_VALUE;
	}
}

void W3DVideoWriter::releaseSurface()
{
	if (m_stagingSurface != nullptr)
	{
		m_stagingSurface->Release();
		m_stagingSurface = nullptr;
	}
}

void W3DVideoWriter::writeSidecar()
{
	if (m_sidecarWritten || m_sidecarPath.empty()) return;
	m_sidecarWritten = true;
	std::ostringstream json;
	json << "{";
	json << "\"schema_version\":1,";
	json << (m_failure == W3D_VIDEO_CAPTURE_OK ? "\"status\":\"success\"," : "\"status\":\"failed\",");
	json << "\"failure_code\":\"" << failureName(m_failure) << "\",";
	json << "\"failure_detail\":" << m_failureDetail << ",";
	json << "\"requested_width\":" << m_requestedWidth << ",";
	json << "\"requested_height\":" << m_requestedHeight << ",";
	json << "\"actual_width\":" << m_actualWidth << ",";
	json << "\"actual_height\":" << m_actualHeight << ",";
	json << "\"fps\":" << m_fps << ",";
	json << "\"logic_frames\":" << m_logicFrames << ",";
	json << "\"presentation_frames\":" << m_presentationFrames << ",";
	json << "\"process_exit_code\":" << m_processExitCode;
	json << "}\n";
	const std::string value = json.str();
	HANDLE sidecar = CreateFileW(m_sidecarPath.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
		FILE_ATTRIBUTE_NORMAL, nullptr);
	if (sidecar == INVALID_HANDLE_VALUE || !writeHandleAll(sidecar, value.data(), value.size())
		|| !FlushFileBuffers(sidecar))
	{
		m_failure = W3D_VIDEO_CAPTURE_SIDECAR_WRITE_FAILED;
		m_failureDetail = GetLastError();
		fprintf(stderr, "Replay video capture: could not write typed sidecar (%lu)\n", m_failureDetail);
	}
	if (sidecar != INVALID_HANDLE_VALUE) CloseHandle(sidecar);
}

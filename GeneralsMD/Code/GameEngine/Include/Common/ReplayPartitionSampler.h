#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include <vector>

struct ReplayPartitionLatticeCell
{
	Int cellX;
	Int cellY;
};

// TheSuperHackers @feature Leex 23/08/2026 Export a bounded deterministic lattice of authoritative partition heuristics without changing simulation state. (#0)
class ReplayPartitionSampler
{
public:
	enum
	{
		MAXIMUM_SAMPLED_CELLS = 128
	};

	static std::vector<ReplayPartitionLatticeCell> selectLatticeCells(Int cellCountX, Int cellCountY)
	{
		std::vector<ReplayPartitionLatticeCell> cells;
		if (cellCountX <= 0 || cellCountY <= 0)
		{
			return cells;
		}

		Int bestSampleCountX = 1;
		Int bestSampleCountY = 1;
		Int bestSampleCount = 1;
		long long bestDistortion = static_cast<long long>(cellCountY) - cellCountX;
		if (bestDistortion < 0)
		{
			bestDistortion = -bestDistortion;
		}

		const Int maximumX = cellCountX < MAXIMUM_SAMPLED_CELLS
			? cellCountX : MAXIMUM_SAMPLED_CELLS;
		// TheSuperHackers @feature Leex 23/08/2026 Choose coverage, aspect fit, and the X tie-break with integer arithmetic so every run selects identical cells. (#0)
		for (Int sampleCountX = 1; sampleCountX <= maximumX; ++sampleCountX)
		{
			const Int maximumYForCapacity = MAXIMUM_SAMPLED_CELLS / sampleCountX;
			const Int maximumY = cellCountY < maximumYForCapacity
				? cellCountY : maximumYForCapacity;
			for (Int sampleCountY = 1; sampleCountY <= maximumY; ++sampleCountY)
			{
				const Int sampleCount = sampleCountX * sampleCountY;
				long long distortion = static_cast<long long>(sampleCountX) * cellCountY
					- static_cast<long long>(sampleCountY) * cellCountX;
				if (distortion < 0)
				{
					distortion = -distortion;
				}
				if (sampleCount > bestSampleCount
					|| (sampleCount == bestSampleCount && distortion < bestDistortion)
					|| (sampleCount == bestSampleCount && distortion == bestDistortion
						&& sampleCountX > bestSampleCountX))
				{
					bestSampleCountX = sampleCountX;
					bestSampleCountY = sampleCountY;
					bestSampleCount = sampleCount;
					bestDistortion = distortion;
				}
			}
		}

		cells.reserve(bestSampleCount);
		for (Int sampleY = 0; sampleY < bestSampleCountY; ++sampleY)
		{
			const Int cellY = bestSampleCountY == 1 ? 0
				: static_cast<Int>(static_cast<long long>(sampleY) * (cellCountY - 1)
					/ (bestSampleCountY - 1));
			for (Int sampleX = 0; sampleX < bestSampleCountX; ++sampleX)
			{
				const Int cellX = bestSampleCountX == 1 ? 0
					: static_cast<Int>(static_cast<long long>(sampleX) * (cellCountX - 1)
						/ (bestSampleCountX - 1));
				cells.push_back(ReplayPartitionLatticeCell{ cellX, cellY });
			}
		}
		return cells;
	}

	static void reset();
	static void sampleEndOfFrame();
	static void emitTerminalSample(UnsignedInt finalFrame);
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

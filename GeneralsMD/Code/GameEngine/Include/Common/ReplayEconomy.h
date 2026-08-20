#pragma once

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/AsciiString.h"
#include "Lib/BaseType.h"

struct Coord3D;
class Object;

enum ReplayCashReason
{
	REPLAY_CASH_UNKNOWN,
	REPLAY_CASH_STARTING,
	REPLAY_CASH_UNIT_COST,
	REPLAY_CASH_UNIT_REFUND,
	REPLAY_CASH_UPGRADE_COST,
	REPLAY_CASH_UPGRADE_REFUND,
	REPLAY_CASH_SCIENCE_COST,
	REPLAY_CASH_SCIENCE_REFUND,
	REPLAY_CASH_CONSTRUCTION_COST,
	REPLAY_CASH_CONSTRUCTION_REFUND,
	REPLAY_CASH_SUPPLY_INCOME,
	REPLAY_CASH_SELL_REFUND,
	REPLAY_CASH_SCRIPT,
	REPLAY_CASH_OTHER
};

// TheSuperHackers @feature Leex 20/08/2026 Limit economy attribution to one proven Money operation. (#TBD)
class ReplayCashReasonScope
{
public:
	explicit ReplayCashReasonScope(ReplayCashReason reason);
	~ReplayCashReasonScope();

private:
	ReplayCashReasonScope(const ReplayCashReasonScope &) = delete;
	ReplayCashReasonScope &operator=(const ReplayCashReasonScope &) = delete;
};

// TheSuperHackers @feature Leex 20/08/2026 Export passive trace-local economy and queue observations without retaining engine pointers. (#TBD)
class ReplayEconomy
{
public:
	static void reset();
	static void initialize();
	static void observeMoneyAttached(Int playerIndex, UnsignedInt balance);
	static void observeCashChanged(Int playerIndex, UnsignedInt before, UnsignedInt after, Bool trackIncome);
	static void observeProductionQueued(const Object *producer, Int engineProductionId,
		const AsciiString &templateName, UnsignedInt queuePosition, UnsignedInt cost, Int quantity);
	static void observeProductionCancelled(const Object *producer, Int engineProductionId);
	static void observeProductionCompleted(const Object *producer, Int engineProductionId);
	static void observeUpgradeQueued(const Object *producer, const AsciiString &upgradeName,
		UnsignedInt queuePosition, UnsignedInt cost);
	static void observeUpgradeCancelled(const Object *producer, const AsciiString &upgradeName);
	static void observeUpgradeCompleted(const Object *producer, const AsciiString &upgradeName);
	static void observeSciencePurchased(Int playerIndex, const AsciiString &scienceName,
		Int purchaseCostPoints, Int pointsBefore, Int pointsAfter);
	static void observeSpecialPowerUsed(const Object *source, const Object *target,
		const AsciiString &powerName, const Coord3D *targetLocation);
	static void observeSupplyPickup(const Object *collector, const Object *source);
	static void observeSupplyCollected(const Object *collector, const Object *dropoff,
		Int playerIndex, UnsignedInt amount, Int deliveredBoxes, const Coord3D *location);
	static AsciiString finalCashBalancesJson();

private:
	friend class ReplayCashReasonScope;
	static void pushCashReason(ReplayCashReason reason);
	static void popCashReason();
};

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

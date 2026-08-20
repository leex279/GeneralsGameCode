#include "PreRTS.h"

#if defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

#include "Common/ReplayEconomy.h"

#include "Common/Money.h"
#include "Common/Player.h"
#include "Common/PlayerList.h"
#include "Common/ReplayEntityLifecycle.h"
#include "Common/ReplayTelemetry.h"
#include "GameLogic/GameLogic.h"
#include "GameLogic/Object.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace
{
	struct CashReasonEntry
	{
		ReplayCashReason reason;
		Bool consumed;
	};

	struct CashEvent
	{
		UnsignedInt frame;
		Int playerIndex;
		UnsignedInt before;
		long long delta;
		UnsignedInt after;
		Bool trackIncome;
		ReplayCashReason reason;
	};

	struct ProductionState
	{
		unsigned long long traceId;
		Int engineId;
		Int producerId;
		Int playerIndex;
		std::string templateName;
		UnsignedInt queuePosition;
		UnsignedInt queuedFrame;
		UnsignedInt cost;
		Int quantity;
		Bool terminal;
	};

	struct UpgradeState
	{
		unsigned long long traceId;
		Int producerId;
		Int playerIndex;
		std::string upgradeName;
		UnsignedInt queuePosition;
		UnsignedInt queuedFrame;
		UnsignedInt cost;
		Bool terminal;
	};

	struct ReplayEconomyState
	{
		std::vector<CashReasonEntry> cashReasons;
		std::vector<CashEvent> pendingCash;
		std::map<std::pair<Int, Int>, ProductionState> production;
		std::map<unsigned long long, UpgradeState> upgrades;
		std::map<std::pair<Int, std::string>, unsigned long long> activeUpgrades;
		std::map<Int, std::vector<Int>> supplySources;
		unsigned long long nextProductionId = 1;
		unsigned long long nextUpgradeId = 1;
		Bool initialized = FALSE;
	};

	ReplayEconomyState s_state;

	UnsignedInt currentFrame()
	{
		return TheGameLogic != nullptr ? TheGameLogic->getFrame() : 0;
	}

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
				if (character >= 0x20 && character < 0x80)
				{
					result.push_back(static_cast<char>(character));
				}
				else if (character >= 0x80)
				{
					result.push_back(static_cast<char>(0xc0 | (character >> 6)));
					result.push_back(static_cast<char>(0x80 | (character & 0x3f)));
				}
				break;
			}
		}
		result.push_back('"');
		return result;
	}

	const char *cashReasonName(ReplayCashReason reason)
	{
		switch (reason)
		{
		case REPLAY_CASH_STARTING: return "starting_cash";
		case REPLAY_CASH_UNIT_COST: return "unit_cost";
		case REPLAY_CASH_UNIT_REFUND: return "unit_refund";
		case REPLAY_CASH_UPGRADE_COST: return "upgrade_cost";
		case REPLAY_CASH_UPGRADE_REFUND: return "upgrade_refund";
		case REPLAY_CASH_SCIENCE_COST: return "science_cost";
		case REPLAY_CASH_SCIENCE_REFUND: return "science_refund";
		case REPLAY_CASH_CONSTRUCTION_COST: return "construction_cost";
		case REPLAY_CASH_CONSTRUCTION_REFUND: return "construction_refund";
		case REPLAY_CASH_SUPPLY_INCOME: return "supply_income";
		case REPLAY_CASH_SELL_REFUND: return "sell_refund";
		case REPLAY_CASH_SCRIPT: return "script";
		case REPLAY_CASH_OTHER: return "other";
		default: return "unknown";
		}
	}

	ReplayCashReason consumeCashReason()
	{
		if (s_state.cashReasons.empty() || s_state.cashReasons.back().consumed)
		{
			return REPLAY_CASH_UNKNOWN;
		}
		s_state.cashReasons.back().consumed = TRUE;
		return s_state.cashReasons.back().reason;
	}

	void emitCash(const CashEvent &event)
	{
		const std::string payload = "{\"player_index\":" + std::to_string(event.playerIndex)
			+ ",\"before\":" + std::to_string(event.before)
			+ ",\"delta\":" + std::to_string(event.delta)
			+ ",\"after\":" + std::to_string(event.after)
			+ ",\"track_income\":" + (event.trackIncome ? "true" : "false")
			+ ",\"reason\":" + jsonString(cashReasonName(event.reason)) + "}";
		ReplayTelemetry::emit(event.frame, "cash_changed", AsciiString(payload.c_str()));
	}

	std::string productionPayload(const ProductionState &state, const char *terminal)
	{
		std::string payload = "{\"production_id\":" + std::to_string(state.traceId)
			+ ",\"engine_production_id\":" + std::to_string(state.engineId)
			+ ",\"producer_object_id\":" + std::to_string(state.producerId)
			+ ",\"player_index\":" + std::to_string(state.playerIndex)
			+ ",\"template_name\":" + jsonString(state.templateName.c_str())
			+ ",\"queue_position\":" + std::to_string(state.queuePosition)
			+ ",\"queued_frame\":" + std::to_string(state.queuedFrame)
			+ ",\"cost\":" + std::to_string(state.cost)
			+ ",\"quantity\":" + std::to_string(state.quantity)
			+ ",\"state\":" + jsonString(terminal);
		if (strcmp(terminal, "queued") != 0)
		{
			payload += ",\"terminal_frame\":" + std::to_string(currentFrame());
		}
		return payload + "}";
	}

	std::string upgradePayload(const UpgradeState &state, const char *terminal)
	{
		std::string payload = "{\"upgrade_queue_id\":" + std::to_string(state.traceId)
			+ ",\"producer_object_id\":" + std::to_string(state.producerId)
			+ ",\"upgrade_name\":" + jsonString(state.upgradeName.c_str())
			+ ",\"player_index\":" + std::to_string(state.playerIndex)
			+ ",\"queue_position\":" + std::to_string(state.queuePosition)
			+ ",\"queued_frame\":" + std::to_string(state.queuedFrame)
			+ ",\"cost\":" + std::to_string(state.cost)
			+ ",\"state\":" + jsonString(terminal);
		if (strcmp(terminal, "queued") != 0)
		{
			payload += ",\"terminal_frame\":" + std::to_string(currentFrame());
		}
		return payload + "}";
	}

	void observeUpgradeTerminal(const Object *producer, const AsciiString &upgradeName,
		const char *eventType, const char *terminal)
	{
		if (!ReplayTelemetry::isEnabled() || producer == nullptr)
		{
			return;
		}
		const std::pair<Int, std::string> key(producer->getID(), upgradeName.str());
		const auto active = s_state.activeUpgrades.find(key);
		if (active == s_state.activeUpgrades.end())
		{
			return;
		}
		auto state = s_state.upgrades.find(active->second);
		if (state == s_state.upgrades.end() || state->second.terminal)
		{
			return;
		}
		ReplayEntityLifecycle::ensureObjectCreated(producer);
		state->second.terminal = TRUE;
		ReplayTelemetry::emit(currentFrame(), eventType,
			AsciiString(upgradePayload(state->second, terminal).c_str()));
		s_state.activeUpgrades.erase(active);
	}

	struct BalanceEntry
	{
		Int playerIndex;
		Bool hasMoney;
		UnsignedInt balance;
	};
}

ReplayCashReasonScope::ReplayCashReasonScope(ReplayCashReason reason)
{
	ReplayEconomy::pushCashReason(reason);
}

ReplayCashReasonScope::~ReplayCashReasonScope()
{
	ReplayEconomy::popCashReason();
}

void ReplayEconomy::pushCashReason(ReplayCashReason reason)
{
	s_state.cashReasons.push_back({ reason, FALSE });
}

void ReplayEconomy::popCashReason()
{
	if (!s_state.cashReasons.empty())
	{
		s_state.cashReasons.pop_back();
	}
}

void ReplayEconomy::reset()
{
	s_state = ReplayEconomyState();
}

void ReplayEconomy::initialize()
{
	if (!ReplayTelemetry::isInitialized() || s_state.initialized)
	{
		return;
	}
	s_state.initialized = TRUE;
	for (const CashEvent &event : s_state.pendingCash)
	{
		emitCash(event);
	}
	s_state.pendingCash.clear();
}

void ReplayEconomy::observeMoneyAttached(Int playerIndex, UnsignedInt balance)
{
	if (!ReplayTelemetry::isEnabled() || balance == 0)
	{
		return;
	}
	const CashEvent event = { currentFrame(), playerIndex, 0, static_cast<long long>(balance),
		balance, FALSE, REPLAY_CASH_STARTING };
	if (s_state.initialized)
	{
		emitCash(event);
	}
	else
	{
		s_state.pendingCash.push_back(event);
	}
}

void ReplayEconomy::observeCashChanged(Int playerIndex, UnsignedInt before, UnsignedInt after, Bool trackIncome)
{
	if (!ReplayTelemetry::isEnabled() || before == after)
	{
		return;
	}
	const CashEvent event = { currentFrame(), playerIndex, before,
		static_cast<long long>(after) - static_cast<long long>(before), after, trackIncome, consumeCashReason() };
	if (s_state.initialized)
	{
		emitCash(event);
	}
	else
	{
		s_state.pendingCash.push_back(event);
	}
}

void ReplayEconomy::observeProductionQueued(const Object *producer, Int engineProductionId,
	const AsciiString &templateName, UnsignedInt queuePosition, UnsignedInt cost, Int quantity)
{
	if (!ReplayTelemetry::isEnabled() || producer == nullptr || engineProductionId <= 0 || quantity <= 0)
	{
		return;
	}
	ReplayEntityLifecycle::ensureObjectCreated(producer);
	ProductionState state = { s_state.nextProductionId++, engineProductionId, producer->getID(),
		producer->getControllingPlayer()->getPlayerIndex(), templateName.str(), queuePosition,
		currentFrame(), cost, quantity, FALSE };
	s_state.production[std::make_pair(state.producerId, state.engineId)] = state;
	ReplayTelemetry::emit(currentFrame(), "production_queued", AsciiString(productionPayload(state, "queued").c_str()));
}

void ReplayEconomy::observeProductionCancelled(const Object *producer, Int engineProductionId)
{
	if (!ReplayTelemetry::isEnabled() || producer == nullptr)
	{
		return;
	}
	auto state = s_state.production.find(std::make_pair(producer->getID(), engineProductionId));
	if (state == s_state.production.end() || state->second.terminal)
	{
		return;
	}
	ReplayEntityLifecycle::ensureObjectCreated(producer);
	state->second.terminal = TRUE;
	ReplayTelemetry::emit(currentFrame(), "production_cancelled",
		AsciiString(productionPayload(state->second, "cancelled").c_str()));
}

void ReplayEconomy::observeProductionCompleted(const Object *producer, Int engineProductionId)
{
	if (!ReplayTelemetry::isEnabled() || producer == nullptr)
	{
		return;
	}
	auto state = s_state.production.find(std::make_pair(producer->getID(), engineProductionId));
	if (state == s_state.production.end() || state->second.terminal)
	{
		return;
	}
	ReplayEntityLifecycle::ensureObjectCreated(producer);
	state->second.terminal = TRUE;
	ReplayTelemetry::emit(currentFrame(), "production_completed",
		AsciiString(productionPayload(state->second, "completed").c_str()));
}

void ReplayEconomy::observeUpgradeQueued(const Object *producer, const AsciiString &upgradeName,
	UnsignedInt queuePosition, UnsignedInt cost)
{
	if (!ReplayTelemetry::isEnabled() || producer == nullptr)
	{
		return;
	}
	ReplayEntityLifecycle::ensureObjectCreated(producer);
	UpgradeState state = { s_state.nextUpgradeId++, producer->getID(),
		producer->getControllingPlayer()->getPlayerIndex(), upgradeName.str(), queuePosition,
		currentFrame(), cost, FALSE };
	s_state.upgrades[state.traceId] = state;
	s_state.activeUpgrades[std::make_pair(state.producerId, state.upgradeName)] = state.traceId;
	ReplayTelemetry::emit(currentFrame(), "upgrade_queued", AsciiString(upgradePayload(state, "queued").c_str()));
}

void ReplayEconomy::observeUpgradeCancelled(const Object *producer, const AsciiString &upgradeName)
{
	observeUpgradeTerminal(producer, upgradeName, "upgrade_cancelled", "cancelled");
}

void ReplayEconomy::observeUpgradeCompleted(const Object *producer, const AsciiString &upgradeName)
{
	observeUpgradeTerminal(producer, upgradeName, "upgrade_completed", "completed");
}

void ReplayEconomy::observeSciencePurchased(Int playerIndex, const AsciiString &scienceName,
	Int purchaseCostPoints, Int pointsBefore, Int pointsAfter)
{
	if (!ReplayTelemetry::isEnabled() || purchaseCostPoints <= 0)
	{
		return;
	}
	const std::string payload = "{\"science_name\":" + jsonString(scienceName.str())
		+ ",\"player_index\":" + std::to_string(playerIndex)
		+ ",\"purchase_cost_points\":" + std::to_string(purchaseCostPoints)
		+ ",\"points_before\":" + std::to_string(pointsBefore)
		+ ",\"points_after\":" + std::to_string(pointsAfter)
		+ ",\"source_object_id\":null}";
	ReplayTelemetry::emit(currentFrame(), "science_purchased", AsciiString(payload.c_str()));
}

void ReplayEconomy::observeSpecialPowerUsed(const Object *source, const Object *target,
	const AsciiString &powerName, const Coord3D *targetLocation)
{
	if (!ReplayTelemetry::isEnabled() || source == nullptr)
	{
		return;
	}
	ReplayEntityLifecycle::ensureObjectCreated(source);
	if (target != nullptr)
	{
		ReplayEntityLifecycle::ensureObjectCreated(target);
	}
	std::string location = "null";
	if (targetLocation != nullptr && std::isfinite(targetLocation->x)
		&& std::isfinite(targetLocation->y) && std::isfinite(targetLocation->z))
	{
		location = "{\"x\":" + std::to_string(targetLocation->x)
			+ ",\"y\":" + std::to_string(targetLocation->y)
			+ ",\"z\":" + std::to_string(targetLocation->z) + "}";
	}
	const std::string payload = "{\"special_power_name\":" + jsonString(powerName.str())
		+ ",\"player_index\":" + std::to_string(source->getControllingPlayer()->getPlayerIndex())
		+ ",\"source_object_id\":" + std::to_string(source->getID())
		+ ",\"target_object_id\":" + (target != nullptr ? std::to_string(target->getID()) : "null")
		+ ",\"target_location\":" + location + "}";
	ReplayTelemetry::emit(currentFrame(), "special_power_used", AsciiString(payload.c_str()));
}

void ReplayEconomy::observeSupplyPickup(const Object *collector, const Object *source)
{
	if (!ReplayTelemetry::isEnabled() || collector == nullptr || source == nullptr)
	{
		return;
	}
	ReplayEntityLifecycle::ensureObjectCreated(collector);
	ReplayEntityLifecycle::ensureObjectCreated(source);
	s_state.supplySources[collector->getID()].push_back(source->getID());
}

void ReplayEconomy::observeSupplyCollected(const Object *collector, const Object *dropoff,
	Int playerIndex, UnsignedInt amount, Int deliveredBoxes, const Coord3D *location)
{
	if (!ReplayTelemetry::isEnabled() || collector == nullptr || dropoff == nullptr || amount == 0
		|| location == nullptr || !std::isfinite(location->x) || !std::isfinite(location->y)
		|| !std::isfinite(location->z))
	{
		return;
	}
	ReplayEntityLifecycle::ensureObjectCreated(collector);
	ReplayEntityLifecycle::ensureObjectCreated(dropoff);
	const auto sources = s_state.supplySources.find(collector->getID());
	const Bool countMatches = sources != s_state.supplySources.end()
		&& deliveredBoxes > 0 && sources->second.size() == static_cast<size_t>(deliveredBoxes);
	Bool oneSource = countMatches;
	Int sourceId = 0;
	if (countMatches)
	{
		sourceId = sources->second.front();
		oneSource = std::all_of(sources->second.begin(), sources->second.end(),
			[sourceId](Int candidate) { return candidate == sourceId; });
	}
	const char *sourceStatus = oneSource ? "resolved" : (countMatches ? "mixed" : "unknown");
	const std::string sourceJson = oneSource ? std::to_string(sourceId) : "null";
	const std::string payload = "{\"collector_object_id\":" + std::to_string(collector->getID())
		+ ",\"source_object_id\":" + sourceJson
		+ ",\"source_status\":" + jsonString(sourceStatus)
		+ ",\"dropoff_object_id\":" + std::to_string(dropoff->getID())
		+ ",\"player_index\":" + std::to_string(playerIndex)
		+ ",\"amount\":" + std::to_string(amount)
		+ ",\"location\":{\"x\":" + std::to_string(location->x)
		+ ",\"y\":" + std::to_string(location->y)
		+ ",\"z\":" + std::to_string(location->z) + "}}";
	ReplayTelemetry::emit(currentFrame(), "supply_collected", AsciiString(payload.c_str()));
	if (sources != s_state.supplySources.end())
	{
		s_state.supplySources.erase(sources);
	}
}

AsciiString ReplayEconomy::finalCashBalancesJson()
{
	std::vector<BalanceEntry> balances;
	if (ThePlayerList != nullptr)
	{
		for (Int index = 0; index < ThePlayerList->getPlayerCount(); ++index)
		{
			Player *player = ThePlayerList->getNthPlayer(index);
			if (player == nullptr)
			{
				continue;
			}
			Money *money = player->getMoney();
			balances.push_back({ player->getPlayerIndex(), money != nullptr,
				money != nullptr ? money->countMoney() : 0 });
		}
	}
	std::sort(balances.begin(), balances.end(), [](const BalanceEntry &left, const BalanceEntry &right) {
		return left.playerIndex < right.playerIndex;
	});
	std::string result = "[";
	for (size_t index = 0; index < balances.size(); ++index)
	{
		if (index != 0)
		{
			result.push_back(',');
		}
		const BalanceEntry &entry = balances[index];
		result += "{\"player_index\":" + std::to_string(entry.playerIndex)
			+ ",\"has_money\":" + (entry.hasMoney ? "true" : "false")
			+ ",\"balance\":" + (entry.hasMoney ? std::to_string(entry.balance) : "null") + "}";
	}
	result.push_back(']');
	return AsciiString(result.c_str());
}

#endif // defined(RTS_REPLAY_ANALYZER) && !defined(IS_VS6_BUILD)

import math
import random
import re

from dnd_rules_engine import (
    BaseGameEntity,
    Creature,
    Weapon,
    WeaponProperty,
    MeleeWeapon,
    EventBus,
    GameEvent,
    EventStatus,
    ModifiableValue,
    NumericalModifier,
    ModifierPriority,
    ActiveCondition,
    roll_dice,
    parse_duration_to_seconds,
)
from spatial_engine import spatial_service, HAS_GIS, TrapDefinition, TerrainZone
from registry import get_entity, get_all_entities
from spell_system import SpellMechanics
from pydantic import ValidationError


def check_death_and_dying(target: Creature, current_hp: int, damage: int, is_critical: bool, results_list: list):
    if damage <= 0:
        return
    if current_hp <= 0:
        fails = 2 if is_critical else 1
        target.death_saves_failures += fails
        target.hp.base_value = 0
        results_list.append(f"[Engine] {target.name} took damage at 0 HP and suffers {fails} Death Save failure(s)!")
        if target.death_saves_failures >= 3:
            target.active_conditions = [c for c in target.active_conditions if c.name not in ["Dying", "Stable"]]
            if not any(c.name == "Dead" for c in target.active_conditions):
                target.active_conditions.append(ActiveCondition(name="Dead"))
            results_list.append(f"[Engine] {target.name} has died from failed death saves.")
    elif target.hp.base_value <= 0:
        if (current_hp - damage) <= -target.max_hp:
            target.hp.base_value = 0
            target.active_conditions = [c for c in target.active_conditions if c.name not in ["Dying", "Stable"]]
            if not any(c.name == "Dead" for c in target.active_conditions):
                target.active_conditions.append(ActiveCondition(name="Dead"))
            results_list.append(f"[Engine] {target.name} takes massive damage and is INSTANTLY KILLED!")
        else:
            target.hp.base_value = 0
            if not any(c.name == "Dying" for c in target.active_conditions):
                target.active_conditions.append(ActiveCondition(name="Dying"))
                target.active_conditions.append(ActiveCondition(name="Unconscious", source_name="0 HP"))
            results_list.append(f"[Engine] {target.name} drops to 0 HP and is Dying/Unconscious.")


def wild_shape_spellblock_handler(event: GameEvent):
    """REQ-CLS-015: Blocks spell casting while Wild Shaped — fires during PRE_EVENT so cancellation propagates."""
    if event.status != EventStatus.PRE_EVENT:
        return
    caster: Creature = get_entity(event.source_uuid)
    if caster and caster.wild_shape_hp > 0:
        print(f"[Engine] {caster.name} is Wild Shaped and cannot cast new spells. (REQ-CLS-015)")
        event.status = EventStatus.CANCELLED
        event.payload["results"] = [f"SYSTEM ERROR: {caster.name} is Wild Shaped and cannot cast spells. (REQ-CLS-015)"]


def resolve_spell_cast_handler(event: GameEvent):  # noqa: C901
    """Calculates spell hits, saving throws, and damage across multiple targets."""
    if event.status != EventStatus.EXECUTION:
        return

    caster: Creature = get_entity(event.source_uuid)
    raw_mechanics = event.payload.get("mechanics", {})

    try:
        mechanics = SpellMechanics.model_validate(raw_mechanics) if isinstance(raw_mechanics, dict) else raw_mechanics
    except ValidationError as e:
        print(f"[Engine] SYSTEM ERROR: Invalid spell mechanics payload - {e}")
        event.status = EventStatus.CANCELLED
        event.payload["results"] = ["SYSTEM ERROR: Invalid spell mechanics payload bypassed the tools. Event cancelled."]
        return

    target_uuids = event.payload.get("target_uuids", [])
    target_wall_ids = event.payload.get("target_wall_ids", [])
    target_terrain_ids = event.payload.get("target_terrain_ids", [])

    # Roll base damage/healing once for all targets
    base_damage = roll_dice(mechanics.damage_dice) if mechanics.damage_dice else 0
    damage_type = mechanics.damage_type if mechanics.damage_type else "unknown"

    save_required = mechanics.save_required.lower() if mechanics.save_required else ""
    requires_attack_roll = mechanics.requires_attack_roll
    half_damage_on_save = mechanics.half_damage_on_save

    if mechanics.requires_concentration:
        if caster.concentrating_on:
            EventBus.dispatch(
                GameEvent(event_type="DropConcentration", source_uuid=caster.entity_uuid, vault_path=event.vault_path)
            )
        caster.concentrating_on = event.payload.get("ability_name", "Unknown")
        print(f"[Engine] {caster.name} is now concentrating on {caster.concentrating_on}.")

    current_init = event.payload.get("current_initiative", 0)

    results = []

    # 0. Apply Elemental Interactions to Environment BEFORE resolving entities!
    if target_terrain_ids:
        for tz_id in target_terrain_ids:
            tz = spatial_service.get_terrain_by_id(tz_id, event.vault_path)
            if not tz:
                continue

            # Fire vs Flammable Thorns/Webs
            if damage_type == "fire" and "flammable" in tz.tags:
                tz.tags.remove("flammable")
                tz.tags.append("burning")
                tz.is_difficult = False
                old_lbl = tz.label
                tz.label += " (Burning)"
                tz.duration_seconds = 6  # Burns away completely in 1 round
                tz.trap = TrapDefinition(
                    hazard_name=f"Burning {old_lbl}",
                    save_required="dexterity",
                    save_dc=13,
                    damage_dice="2d4",
                    damage_type="fire",
                    trigger_on_move=True,
                )
                results.append(f"[Environment] The {old_lbl} caught fire and will burn for 1 round!")

                # Deal immediate damage to entities currently caught inside the flammable area
                if tz.polygon:
                    for uid, ent in get_all_entities(event.vault_path).items():
                        if getattr(ent, "hp", None) and ent.hp.base_value > 0:
                            ent_poly = spatial_service._get_entity_bbox(ent)
                            if ent_poly and tz.polygon.intersects(ent_poly):
                                dmg = roll_dice("2d4")
                                ent.hp.base_value -= dmg
                                results.append(f"[Environment] {ent.name} took {dmg} fire damage from the burning {old_lbl}!")

            # Fire vs Frozen Ice
            elif damage_type == "fire" and "frozen" in tz.tags:
                tz.tags.remove("frozen")
                if "wet" not in tz.tags:
                    tz.tags.append("wet")
                tz.is_difficult = False
                tz.label = tz.label.replace("Frozen", "Wet").replace("Ice", "Water")
                results.append("[Environment] The frozen terrain melted into a wet puddle!")

            # Cold vs Wet Water
            elif damage_type == "cold" and "wet" in tz.tags:
                tz.tags.remove("wet")
                if "frozen" not in tz.tags:
                    tz.tags.append("frozen")
                tz.is_difficult = True
                tz.label = tz.label.replace("Wet", "Frozen").replace("Water", "Ice")
                results.append("[Environment] The wet terrain froze solid!")

            # Lightning vs Wet Water -> Amplifies AoE!
            elif damage_type == "lightning" and "wet" in tz.tags:
                results.append(f"[Environment] The {tz.label} was electrified by lightning!")
                if tz.polygon:
                    for uid, ent in get_all_entities(event.vault_path).items():
                        if uid not in target_uuids and getattr(ent, "hp", None) and ent.hp.base_value > 0:
                            ent_poly = spatial_service._get_entity_bbox(ent)
                            if ent_poly and tz.polygon.intersects(ent_poly):
                                target_uuids.append(uid)
                                results.append(
                                    f"[Environment] {ent.name} was pulled into the area of effect via electrified water!"
                                )

            # Wind vs Gaseous Cloud (e.g. Gust of Wind pushing fog)
            elif (damage_type == "wind" or event.payload.get("ability_name") == "Gust of Wind") and "gaseous" in tz.tags:
                ox, oy = event.payload.get("origin_x", 0.0), event.payload.get("origin_y", 0.0)
                tx, ty = event.payload.get("target_x", 0.0), event.payload.get("target_y", 0.0)

                dx, dy = tx - ox, ty - oy
                dist = math.hypot(dx, dy)
                if dist > 0:
                    vx, vy = (dx / dist) * 20.0, (dy / dist) * 20.0
                    tz.points = [(p[0] + vx, p[1] + vy) for p in tz.points]
                    spatial_service.invalidate_cache(event.vault_path)
                    results.append(f"[Environment] The {tz.label} was blown 20 feet away by the wind!")

    for t_uuid in target_uuids:
        target: Creature = get_entity(t_uuid)
        if not target:
            continue

        target_damage = base_damage
        hit_or_save_str = "Auto-hit"
        dc = event.payload.get("save_dc_override") or (caster.spell_save_dc.total if hasattr(caster, "spell_save_dc") else 10)

        # 1. Spell Attack Roll
        if requires_attack_roll:
            attack_roll = random.randint(1, 20)
            total_attack = attack_roll + caster.spell_attack_bonus.total
            if total_attack >= target.ac.total or attack_roll == 20:
                hit_or_save_str = f"Hit (Rolled {total_attack} vs AC {target.ac.total})"
                if attack_roll == 20:
                    target_damage += roll_dice(mechanics.damage_dice)  # Crit
                    hit_or_save_str = "Critical Hit!"
            else:
                target_damage = 0
                hit_or_save_str = f"Miss (Rolled {total_attack} vs AC {target.ac.total})"

        # 2. Saving Throw
        elif save_required:
            save_mod_val = getattr(target, f"{save_required}_mod").total if hasattr(target, f"{save_required}_mod") else 0

            # Handle Advantage / Disadvantage
            has_adv = event.payload.get("advantage", False)
            has_disadv = event.payload.get("disadvantage", False)

            # Evaluate Advanced Condition Framework for Saves
            active_conds = [c.name.lower() for c in target.active_conditions]
            auto_fail = False

            if save_required in ["dexterity", "strength"]:
                if any(cond in active_conds for cond in ["stunned", "paralyzed", "petrified", "unconscious", "incapacitated"]):
                    auto_fail = True
            if save_required == "dexterity" and any(c in active_conds for c in ["restrained", "squeezing"]):
                has_disadv = True

            roll1 = random.randint(1, 20)
            roll2 = random.randint(1, 20)

            if has_adv and not has_disadv:
                save_roll = max(roll1, roll2)
            elif has_disadv and not has_adv:
                save_roll = min(roll1, roll2)
            else:
                save_roll = roll1

            if auto_fail:
                total_save = -99  # Guaranteed failure
                save_roll = 1
            else:
                total_save = save_roll + save_mod_val - (target.exhaustion_level * 2)

            is_success = total_save >= dc

            # Dispatch a sub-event to allow traits (like Evasion) to intercept the math
            save_event = GameEvent(
                event_type="SavingThrow",
                source_uuid=caster.entity_uuid,
                target_uuid=target.entity_uuid,
                payload={
                    "save_required": save_required,
                    "dc": dc,
                    "roll": total_save,
                    "is_success": is_success,
                    "base_damage": target_damage,
                    "half_damage_on_save": half_damage_on_save,
                    "final_damage": (
                        target_damage // 2 if (is_success and half_damage_on_save) else (0 if is_success else target_damage)
                    ),
                },
            )
            EventBus.dispatch(save_event)

            is_success = save_event.payload["is_success"]
            target_damage = save_event.payload["final_damage"]

            if is_success:
                hit_or_save_str = f"Saved (Rolled {save_event.payload['roll']} vs DC {dc})"
            else:
                hit_or_save_str = f"Failed Save (Rolled {save_event.payload['roll']} vs DC {dc})"

        results.append(f"[{target.name}] {hit_or_save_str}.")

        # 3. Process Damage
        if target_damage > 0:
            event.payload["hit"] = True  # Flag for potential reaction handlers
            is_crit = event.payload.get("is_critical", False)
            if requires_attack_roll and "Critical" in hit_or_save_str:
                is_crit = True

            damage_event = GameEvent(
                event_type="ApplyDamage",
                source_uuid=caster.entity_uuid,
                target_uuid=target.entity_uuid,
                vault_path=event.vault_path,
                payload={
                    "damage": target_damage,
                    "damage_type": damage_type,
                    "critical": is_crit,
                    "source_name": event.payload.get("ability_name", "Unknown Spell")
                }
            )
            EventBus.dispatch(damage_event)
            results.extend(damage_event.payload.get("results", []))
            target_damage = damage_event.payload.get("final_damage_applied", target_damage)

        # 4. Process Conditions
        applied_effects = False
        if requires_attack_roll and ("Hit" in hit_or_save_str or "Critical" in hit_or_save_str):
            applied_effects = True
        elif save_required and "Failed Save" in hit_or_save_str:
            applied_effects = True
        elif not requires_attack_roll and not save_required:
            applied_effects = True

        if applied_effects:
            for cond_data in mechanics.conditions_applied:
                cond_name = cond_data.condition
                duration_secs = parse_duration_to_seconds(cond_data.duration)
                eot_save = getattr(cond_data, "end_of_turn_save", False)
                s_timing = "start" if getattr(cond_data, "start_of_turn_save", False) else "end"
                sot_thp = getattr(cond_data, "start_of_turn_thp", 0)
                eot_dmg_dice = getattr(cond_data, "end_of_turn_damage_dice", "")
                eot_dmg_type = getattr(cond_data, "end_of_turn_damage_type", "")
                s_req = save_required if eot_save else ""
                s_dc = dc if eot_save else 0

                target.active_conditions.append(
                    ActiveCondition(
                        name=cond_name,
                        duration_seconds=duration_secs,
                        source_name=event.payload.get("ability_name", "Unknown"),
                        applied_initiative=current_init,
                        source_uuid=caster.entity_uuid,
                        save_required=s_req,
                        save_dc=s_dc,
                        save_timing=s_timing,
                        start_of_turn_thp=sot_thp,
                        end_of_turn_damage_dice=eot_dmg_dice,
                        end_of_turn_damage_type=eot_dmg_type.lower(),
                    )
                )
                results.append(f"[{target.name}] is now {cond_name}!")

            for mod_data in mechanics.modifiers:
                target_stat = mod_data.stat
                duration_secs = parse_duration_to_seconds(mod_data.duration)
                if hasattr(target, target_stat):
                    stat_obj = getattr(target, target_stat)
                    if isinstance(stat_obj, ModifiableValue):
                        stat_obj.add_modifier(
                            NumericalModifier(
                                priority=ModifierPriority.ADDITIVE,
                                value=mod_data.value,
                                source_name=event.payload.get("ability_name", "Unknown"),
                                duration_seconds=duration_secs,
                                applied_initiative=current_init,
                                source_uuid=caster.entity_uuid,
                            )
                        )
                        if event.payload.get("ability_name", "Unknown") not in target.active_mechanics:
                            target.active_mechanics.append(event.payload.get("ability_name", "Unknown"))
                        results.append(f"[{target.name}] gained modifier to {target_stat}.")

        # 5. Process Healing (e.g. Second Wind, Cure Wounds)
        if mechanics.healing_dice:
            heal_amount = roll_dice(mechanics.healing_dice)
            old_hp = target.hp.base_value
            target.hp.base_value = min(target.max_hp, target.hp.base_value + heal_amount)
            actual_heal = target.hp.base_value - old_hp
            results.append(
                f"[{target.name}] healed for {actual_heal} HP (now {target.hp.base_value}/{target.max_hp})."
            )
            print(f"[Engine] {target.name} healed for {actual_heal} HP.")
            # REQ-DTH-006: Healing at 0 HP removes Dying/Stable/Unconscious(0HP) conditions
            if actual_heal > 0:
                target.active_conditions = [
                    c for c in target.active_conditions
                    if c.name not in ["Dying", "Stable"] and not (c.name == "Unconscious" and c.source_name == "0 HP")
                ]
                target.death_saves_successes = 0
                target.death_saves_failures = 0

    # 6. Process Collateral Damage to Geography (Walls/Doors)
    if base_damage > 0 and target_wall_ids and damage_type not in ["poison", "psychic"]:
        for wall_id in target_wall_ids:
            wall = spatial_service.get_wall_by_id(wall_id)
            if wall and wall.hp is not None and wall.is_solid:
                w_dmg = base_damage
                if damage_type in wall.immunities:
                    w_dmg = 0
                elif damage_type in wall.vulnerabilities:
                    w_dmg *= 2
                elif damage_type in wall.resistances:
                    w_dmg //= 2

                if w_dmg < wall.damage_threshold:
                    w_dmg = 0

                if w_dmg > 0:
                    wall.hp -= w_dmg
                    if wall.hp <= 0:
                        wall.is_solid = False
                        old_label = wall.label
                        wall.label += " (Destroyed)"
                        results.append(
                            f"[Geometry] The {old_label} was hit for {w_dmg} {damage_type} damage and was DESTROYED!"
                        )
                    else:
                        results.append(
                            f"[Geometry] The {wall.label} took {w_dmg} {damage_type} damage "
                            f"({wall.hp}/{wall.max_hp} HP remaining)."
                        )

    # 6. Apply Environmental/Terrain Modifications
    terrain_def = getattr(mechanics, "terrain_effect", None)
    if terrain_def and HAS_GIS:
        aoe_shape = event.payload.get("aoe_shape")
        if aoe_shape:
            ox, oy = event.payload.get("origin_x", 0), event.payload.get("origin_y", 0)
            tx, ty = event.payload.get("target_x"), event.payload.get("target_y")
            size = event.payload.get("aoe_size", 0)

            points = spatial_service.get_shape_points(aoe_shape, size, ox, oy, tx, ty)
            if points:
                trap = TrapDefinition(**terrain_def.trap_hazard) if terrain_def.trap_hazard else None
                dur_secs = parse_duration_to_seconds(terrain_def.duration)

                tz = TerrainZone(
                    label=terrain_def.label,
                    points=points,
                    z=event.payload.get("origin_z") or 0.0,
                    is_difficult=terrain_def.is_difficult,
                    tags=terrain_def.tags,
                    trap=trap,
                    source_name=event.payload.get("ability_name", "Unknown"),
                    source_uuid=caster.entity_uuid,
                    duration_seconds=dur_secs,
                    applied_initiative=current_init,
                )
                spatial_service.add_terrain(tz, is_temporary=True, vault_path=event.vault_path)
                results.append(
                    f"[Environment] A {terrain_def.label} effect was created in the area (Duration: {terrain_def.duration})."
                )

    event.payload["results"] = results


def resolve_attack_handler(event: GameEvent):  # noqa: C901
    """Calculates if an attack hits and its base damage. Listens to EXECUTION phase."""
    if event.status != EventStatus.EXECUTION:
        return

    attacker: Creature = get_entity(event.source_uuid)
    target: Creature = get_entity(event.target_uuid)
    weapon: Weapon = get_entity(attacker.equipped_weapon_uuid)

    if not weapon:
        # Fallback for entities missing a valid weapon in the registry
        weapon = MeleeWeapon(name="Unarmed Strike", damage_dice="1d4", damage_type="bludgeoning")

    attack_mod = weapon.get_attack_modifier(attacker)
    attack_bonus = attack_mod.total + weapon.magic_bonus

    if WeaponProperty.HEAVY in weapon.properties and any(t.lower() in ["small", "tiny"] for t in attacker.tags):
        print(f"[Engine] {attacker.name} is Small/Tiny and wielding a Heavy weapon. Applying DISADVANTAGE.")
        event.payload["disadvantage"] = True

    # REQ-ENV-004/005: Underwater combat penalties
    is_underwater = any(t.lower() in ["underwater", "submerged"] for t in attacker.tags) or any(
        c.name.lower() in ["underwater", "submerged"] for c in attacker.active_conditions
    )
    has_swim_speed = "swimming_speed" in attacker.tags
    weapon_is_underwater_safe = WeaponProperty.UNDERWATER_SAFE in weapon.properties

    # Evaluate Spatial Logic: Range & Cover
    dist, cover = spatial_service.get_distance_and_cover(attacker.entity_uuid, target.entity_uuid, event.vault_path)

    if cover == "None":
        # REQ-GEO-012: Intervening creatures provide Half Cover
        interveners = spatial_service.get_intervening_creatures(attacker.entity_uuid, target.entity_uuid, event.vault_path)
        if interveners:
            print(f"[Engine] Intervening creatures detected between {attacker.name} and {target.name}. Applying Half Cover.")
            cover = "Half"

    if cover in ["Half", "Three-Quarters"] and any(
        tag in attacker.tags for tag in ["sharpshooter", "ignores_cover", "spell_sniper"]
    ):
        print(f"[Engine] {attacker.name} has a feat that ignores {cover} cover!")
        cover = "None"

    if cover == "Total":
        print(f"[Engine] {attacker.name} cannot hit {target.name}. Target has TOTAL COVER.")
        event.payload["hit"] = False
        return

    # Enforce Range Limits for Ranged Weapons
    if hasattr(weapon, "normal_range") and hasattr(weapon, "long_range"):
        if dist > weapon.long_range:
            print(
                f"[Engine] {attacker.name} cannot hit {target.name}. Target is out of "
                f"maximum range ({dist:.1f}ft > {weapon.long_range}ft)."
            )
            event.payload["hit"] = False
            return
        elif dist > weapon.normal_range:
            print(f"[Engine] Target is at long range ({dist:.1f}ft > {weapon.normal_range}ft). Applying DISADVANTAGE.")
            event.payload["disadvantage"] = True

        # REQ-ENV-005: Underwater ranged — auto-miss beyond normal range (already handled above),
        # Disadvantage within normal range unless weapon is underwater-safe
        if is_underwater and not weapon_is_underwater_safe and not has_swim_speed:
            print(f"[Engine] {attacker.name} is underwater with a non-aquatic ranged weapon. Applying DISADVANTAGE.")
            event.payload["disadvantage"] = True

        # Check for hostile creatures within 5 feet of the attacker's edge
        check_radius = (attacker.size / 2.0) + 5.0
        nearby_uuids = spatial_service.get_targets_in_radius(attacker.x, attacker.y, check_radius, event.vault_path)
        for uid in nearby_uuids:
            if uid == attacker.entity_uuid:
                continue
            nearby_ent = get_entity(uid)
            if isinstance(nearby_ent, Creature) and nearby_ent.hp.base_value > 0:
                is_pc = any(t in attacker.tags for t in ["pc", "player", "party_npc"])
                is_nearby_pc = any(t in nearby_ent.tags for t in ["pc", "player", "party_npc"])
                if is_pc != is_nearby_pc:
                    # Generic Mechanics Check
                    if "ignore_ranged_melee_disadvantage" in attacker.tags:
                        print(
                            f"[Engine] {attacker.name} has a mechanic mitigating " f"ranged disadvantage from nearby hostiles."
                        )
                    else:
                        print(
                            f"[Engine] Hostile creature ({nearby_ent.name}) is within 5 feet of "
                            f"{attacker.name}. Applying DISADVANTAGE to ranged attack."
                        )
                        event.payload["disadvantage"] = True
                break
    else:
        # REQ-ENV-004: Underwater melee — Disadvantage unless weapon is underwater-safe or attacker has swim speed
        if is_underwater and not weapon_is_underwater_safe and not has_swim_speed:
            print(f"[Engine] {attacker.name} is underwater with a non-aquatic melee weapon. Applying DISADVANTAGE.")
            event.payload["disadvantage"] = True

        # Enforce Reach Limits for Melee Weapons
        # Multiply by 1.5 to safely account for Euclidean distance on diagonals (5ft reach allows ~7.07ft diagonal distance)
        base_reach = 10.0 if WeaponProperty.REACH in weapon.properties else 5.0
        is_oa = event.payload.get("is_opportunity_attack", False)
        if dist > (base_reach * 1.5) and not is_oa:
            print(
                f"[Engine] {attacker.name} cannot hit {target.name}. Target is out of "
                f"melee reach ({dist:.1f}ft > {base_reach}ft)."
            )
            event.payload["hit"] = False
            return

    # Evaluate Spatial Logic: Illumination & Visibility
    attacker_illum = spatial_service.get_illumination(attacker.x, attacker.y, attacker.z, event.vault_path)
    target_illum = spatial_service.get_illumination(target.x, target.y, target.z, event.vault_path)

    def can_perceive(observer: Creature, target_ent: Creature, distance: float, target_illumination: str) -> bool:
        def get_sense_range(sense_name: str) -> float:
            for t in observer.tags:
                if t == sense_name:
                    return 60.0
                if t.startswith(f"{sense_name}_"):
                    try:
                        return float(t.split("_")[1])
                    except Exception:
                        return 60.0
            return 0.0

        # 1. Blindsight (Echolocation vs Deafened check)
        bs_range = get_sense_range("blindsight")
        if distance <= bs_range:
            is_echo = any("echolocation" in t.lower() for t in observer.tags)
            is_deaf = any(c.name.lower() == "deafened" for c in observer.active_conditions)
            if not (is_echo and is_deaf):
                return True

        # 2. Blinded Condition
        if any(c.name.lower() == "blinded" for c in observer.active_conditions):
            return False

        # 3. Truesight
        if distance <= get_sense_range("truesight"):
            return True

        # 4. Invisibility / Stealth
        target_invisible = "invisible" in target_ent.tags or any(
            c.name.lower() in ["invisible", "hidden"] for c in target_ent.active_conditions
        )
        if target_invisible:
            return False

        # 5. Devil's Sight & Standard Illumination
        if target_illumination == "darkness":
            ds_range = get_sense_range("devils_sight")
            if distance <= ds_range:
                pass  # Devil's sight perfectly penetrates both magical and non-magical darkness
            elif distance > get_sense_range("darkvision"):
                return False

        return True

    attacker_can_see_target = can_perceive(attacker, target, dist, target_illum)
    target_can_see_attacker = can_perceive(target, attacker, dist, attacker_illum)

    if not attacker_can_see_target:
        print(f"[Engine] {target.name} is unseen by {attacker.name}. Applying DISADVANTAGE.")
        event.payload["disadvantage"] = True

    if not target_can_see_attacker:
        print(f"[Engine] {attacker.name} is unseen by {target.name}. Applying ADVANTAGE.")
        event.payload["advantage"] = True

    if "sunlight_sensitivity" in attacker.tags:
        in_sunlight = False
        if attacker_illum == "bright" or target_illum == "bright":
            for light in spatial_service.get_map_data(event.vault_path).active_lights:
                if "sun" in light.label.lower():
                    d1 = spatial_service.calculate_distance(
                        attacker.x, attacker.y, attacker.z, light.x, light.y, light.z, event.vault_path
                    )
                    d2 = spatial_service.calculate_distance(
                        target.x, target.y, target.z, light.x, light.y, light.z, event.vault_path
                    )
                    if d1 <= light.bright_radius or d2 <= light.bright_radius:
                        in_sunlight = True
                        break
        if in_sunlight:
            print(f"[Engine] {attacker.name} has Sunlight Sensitivity and is in sunlight. Applying DISADVANTAGE.")
            event.payload["disadvantage"] = True

    # Evaluate Advanced Condition Framework for Attacks
    attacker_conds = [c.name.lower() for c in attacker.active_conditions]
    target_conds = [c.name.lower() for c in target.active_conditions]

    if any(c in attacker_conds for c in ["restrained", "poisoned", "prone", "frightened", "squeezing"]):
        print(f"[Engine] {attacker.name} is hampered by a condition. Applying DISADVANTAGE to attack.")
        event.payload["disadvantage"] = True

    # REQ-CND-007: Grappled 2024 — disadvantage on attacks against non-grappler
    grappled_conds = [c for c in attacker.active_conditions if c.name.lower() == "grappled"]
    if grappled_conds:
        grappler_uuids = {c.source_uuid for c in grappled_conds}
        if target.entity_uuid not in grappler_uuids:
            print(f"[Engine] {attacker.name} is Grappled and attacking a non-grappler. Applying DISADVANTAGE.")
            event.payload["disadvantage"] = True

    if "reckless" in attacker_conds:
        print(f"[Engine] {attacker.name} is attacking Recklessly. Applying ADVANTAGE to attack.")
        event.payload["advantage"] = True

    if "reckless" in target_conds:
        print(f"[Engine] {target.name} attacked Recklessly. Applying ADVANTAGE to attackers.")
        event.payload["advantage"] = True

    if any(
        c in target_conds for c in ["restrained", "stunned", "paralyzed", "petrified", "unconscious", "blinded", "squeezing"]
    ):
        print(f"[Engine] {target.name} has a debilitating condition. Applying ADVANTAGE to attackers.")
        event.payload["advantage"] = True

    # REQ-CND-010/011/016: Paralyzed/Petrified/Unconscious target auto-crits on hit within 5ft
    if any(c in target_conds for c in ["paralyzed", "petrified", "unconscious"]) and dist <= 5.0:
        event.payload["_auto_crit_on_hit"] = True

    if "prone" in target_conds:
        if dist <= 5.0:
            event.payload["advantage"] = True
        else:
            event.payload["disadvantage"] = True

    vex_conds = [c for c in target.active_conditions if c.name.lower() == "vexed" and c.source_uuid == attacker.entity_uuid]
    if vex_conds:
        print(f"[Engine] {attacker.name} benefits from Vex against {target.name}. Applying ADVANTAGE.")
        event.payload["advantage"] = True
        for v in vex_conds:
            target.active_conditions.remove(v)

    # REQ-CND-005: Charmed attacker cannot attack their charmer
    charmed_by_target = [
        c for c in attacker.active_conditions
        if c.name.lower() == "charmed" and c.source_uuid == target.entity_uuid
    ]
    if charmed_by_target:
        print(f"[Engine] {attacker.name} is Charmed by {target.name} and cannot attack them.")
        event.payload["hit"] = False
        return

    cover_ac_bonus = 2 if cover == "Half" else (5 if cover == "Three-Quarters" else 0)
    target_ac = target.ac.total + cover_ac_bonus
    cover_msg = f" (Includes +{cover_ac_bonus} {cover} Cover)" if cover_ac_bonus > 0 else ""

    manual_roll = event.payload.get("manual_roll_total")
    is_critical_hit = event.payload.get("is_critical", False)

    if manual_roll is not None:
        total_attack = manual_roll
        is_hit = is_critical_hit or total_attack >= target_ac
        print(f"[Engine] {attacker.name} manually rolled a total of {total_attack} vs AC {target_ac}{cover_msg}")
    else:

        roll1, roll2 = random.randint(1, 20), random.randint(1, 20)
        has_adv = event.payload.get("advantage", False)
        has_disadv = event.payload.get("disadvantage", False)

        if has_adv and has_disadv:
            d20_roll = roll1
        elif has_adv:
            d20_roll = max(roll1, roll2)
        elif has_disadv:
            d20_roll = min(roll1, roll2)
        else:
            d20_roll = roll1

        exh_penalty = attacker.exhaustion_level * 2
        total_attack = d20_roll + attack_bonus - exh_penalty
        is_critical_hit = d20_roll == 20
        is_hit = is_critical_hit or total_attack >= target_ac
        exh_str = f" - {exh_penalty} (Exhaustion)" if exh_penalty > 0 else ""
        print(
            f"[Engine] {attacker.name} rolls a {d20_roll} ({roll1}, {roll2} if adv/disadv) "
            f"+ {attack_bonus}{exh_str} = {total_attack} vs AC {target_ac}{cover_msg}"
        )

    # REQ-CND-010/011: Apply auto-crit for Paralyzed/Petrified target within 5ft
    if event.payload.get("_auto_crit_on_hit") and is_hit:
        is_critical_hit = True
        print(f"[Engine] AUTOMATIC CRITICAL HIT — {target.name} is paralyzed/petrified and within 5ft.")

    # Attacking reveals the attacker
    hidden_conds = [c for c in attacker.active_conditions if c.name.lower() == "hidden"]
    if hidden_conds:
        for c in hidden_conds:
            attacker.active_conditions.remove(c)
        print(f"[Engine] {attacker.name} reveals themselves by attacking. 'Hidden' condition removed.")

    if is_hit:
        if is_critical_hit:
            print("[Engine] CRITICAL HIT!")
        print("[Engine] HIT!")

        damage_mod = weapon.get_damage_modifier(attacker)
        # Roll base damage dice; Cleave suppresses ability mod unless negative
        mod_total = damage_mod.total
        if event.payload.get("suppress_ability_mod_damage"):
            mod_total = min(0, mod_total)
        base_damage = roll_dice(weapon.damage_dice) + mod_total + weapon.magic_bonus

        # Add conditional damage dice
        extra_damage = 0
        if "extra_damage_dice" in event.payload:
            for dice in event.payload["extra_damage_dice"]:
                extra_damage += roll_dice(dice)

        total_damage = base_damage + extra_damage

        # Double all dice on a critical hit
        if is_critical_hit:
            crit_damage = roll_dice(weapon.damage_dice)
            if "extra_damage_dice" in event.payload:
                for dice in event.payload["extra_damage_dice"]:
                    crit_damage += roll_dice(dice)
            total_damage += crit_damage

        # REQ-CLS-003/004: Sneak Attack — auto-detect eligibility
        sneak_tag = next((t for t in attacker.tags if t.startswith("sneak_attack_")), None)
        if sneak_tag:
            is_finesse = WeaponProperty.FINESSE in weapon.properties
            is_ranged = hasattr(weapon, "normal_range")
            if is_finesse or is_ranged:
                has_adv = event.payload.get("advantage", False)
                has_disadv = event.payload.get("disadvantage", False)
                # Advantage and disadvantage cancel each other (5e rule).
                # Sneak Attack is only blocked when there is NET disadvantage (disadv without any adv).
                net_disadv = has_disadv and not has_adv
                net_adv = has_adv and not has_disadv
                if not net_disadv:
                    # Eligible if: net advantage OR a conscious ally is within 5ft of the target
                    sa_eligible = net_adv
                    if not sa_eligible:
                        is_attacker_pc = any(t in attacker.tags for t in ["pc", "player", "party_npc"])
                        for uid, ent in get_all_entities(event.vault_path).items():
                            if uid in (attacker.entity_uuid, target.entity_uuid):
                                continue
                            if not isinstance(ent, Creature) or ent.hp.base_value <= 0:
                                continue
                            is_ent_pc = any(t in ent.tags for t in ["pc", "player", "party_npc"])
                            if is_attacker_pc != is_ent_pc:
                                continue  # skip hostiles
                            ent_conds = [c.name.lower() for c in ent.active_conditions]
                            if any(c in ent_conds for c in ["incapacitated", "unconscious", "dead", "dying"]):
                                continue
                            ally_dist = spatial_service.calculate_distance(
                                ent.x, ent.y, ent.z, target.x, target.y, target.z, event.vault_path
                            )
                            if ally_dist <= 7.5:  # 5ft + diagonal allowance
                                sa_eligible = True
                                break

                    sneak_resource = attacker.resources.get("Sneak Attack", "0/1")
                    m = re.match(r"(\d+)/(\d+)", str(sneak_resource))
                    sneak_unused = not m or int(m.group(1)) == 0
                    if sa_eligible and sneak_unused:
                        dice_str = sneak_tag.replace("sneak_attack_", "")
                        sneak_dmg = roll_dice(dice_str)
                        if is_critical_hit:
                            sneak_dmg += roll_dice(dice_str)
                        total_damage += sneak_dmg
                        attacker.resources["Sneak Attack"] = "1/1"
                        print(f"[Engine] SNEAK ATTACK! +{sneak_dmg} ({dice_str}) extra damage.")
                        event.payload.setdefault("results", []).append(
                            f"[Engine] SNEAK ATTACK! {attacker.name} deals {sneak_dmg} extra sneak attack damage."
                        )

        # REQ-CLS-001: Rage maintenance — mark that the raging entity attacked this cycle
        if any(c.name.lower() == "raging" for c in attacker.active_conditions):
            attacker.resources["Raged This Cycle"] = "1/1"

        # REQ-CLS-005: Divine Smite prompt — alert after a melee hit
        is_melee = not hasattr(weapon, "normal_range")
        if is_melee and "divine_smite" in attacker.tags and attacker.spell_slots_expended_this_turn == 0:
            event.payload.setdefault("results", []).append(
                "[Engine] SYSTEM ALERT: Melee hit — Divine Smite (Bonus Action) is available. "
                "Call use_ability_or_spell with 'Divine Smite' to apply smite damage."
            )

        event.payload["hit"] = True
        event.payload["damage"] = total_damage
        event.payload["damage_type"] = weapon.damage_type
        event.payload["critical"] = is_critical_hit
    else:
        print(f"[Engine] MISS! The attack glances off {target.name}'s armor.")
        event.payload["hit"] = False

    # --- Sentinel / Protector Reaction Check ---
    protectors = []
    for uid, pot_protector in get_all_entities(event.vault_path).items():
        if uid == attacker.entity_uuid or uid == target.entity_uuid:
            continue
        if not isinstance(pot_protector, Creature) or pot_protector.hp.base_value <= 0:
            continue

        has_reaction = not pot_protector.reaction_used
        has_legendary = pot_protector.legendary_actions_current > 0
        if not has_reaction and not has_legendary:
            continue

        if "protector_reaction_attack" not in pot_protector.tags:
            continue

        # Must be friendly to target, hostile to attacker
        is_attacker_pc = any(t in attacker.tags for t in ["pc", "player", "party_npc"])
        is_protector_pc = any(t in pot_protector.tags for t in ["pc", "player", "party_npc"])
        if is_attacker_pc == is_protector_pc:
            continue

        # Must be within 5ft of the attacker
        dist_to_attacker = spatial_service.calculate_distance(
            pot_protector.x, pot_protector.y, pot_protector.z, attacker.x, attacker.y, attacker.z, event.vault_path
        )
        if dist_to_attacker <= 7.5:  # 5ft + diagonal allowance
            protectors.append(pot_protector.name)

    if protectors:
        event.payload["protector_alerts"] = protectors

    # --- Evaluate Weapon Masteries (Generic Hooks) ---
    def _trigger_weapon_mastery(
        attacker_ent: Creature, target_ent: Creature, wpn: Weapon, mech: dict, parent_event: GameEvent
    ):
        mod_val = wpn.get_attack_modifier(attacker_ent).total
        mastery_type = mech.get("mastery_type", "")
        results_out = parent_event.payload.setdefault("results", [])

        # REQ-MST-001: Cleave — extra attack against adjacent creature, no ability mod on damage
        if mastery_type == "cleave":
            # Once per turn guard
            if attacker_ent.resources.get("Cleave Used") == "1/1":
                return
            # Find a hostile creature within 5ft of the primary target and within attacker reach
            reach = wpn.reach if hasattr(wpn, "reach") else 5.0
            all_ents = get_all_entities(parent_event.vault_path)
            cleave_target = None
            for ent in all_ents.values():
                if not isinstance(ent, Creature):
                    continue
                if ent.entity_uuid in (attacker_ent.entity_uuid, target_ent.entity_uuid):
                    continue
                if ent.hp.base_value <= 0:
                    continue
                # Must be hostile (different faction tag)
                attacker_is_pc = any(t in attacker_ent.tags for t in ["pc", "player", "party_npc"])
                ent_is_pc = any(t in ent.tags for t in ["pc", "player", "party_npc"])
                if attacker_is_pc == ent_is_pc:
                    continue
                # Within 5ft of primary target AND within attacker's reach
                dist_from_target = spatial_service.calculate_distance(
                    target_ent.x, target_ent.y, target_ent.z, ent.x, ent.y, ent.z, parent_event.vault_path
                )
                dist_from_attacker = spatial_service.calculate_distance(
                    attacker_ent.x, attacker_ent.y, attacker_ent.z, ent.x, ent.y, ent.z, parent_event.vault_path
                )
                if dist_from_target <= 7.5 and dist_from_attacker <= reach + 2.5:
                    cleave_target = ent
                    break

            if cleave_target is None:
                results_out.append(f"[Cleave Mastery] No adjacent target available for Cleave.")
                return

            # Roll the extra attack (no ability mod on damage unless negative)
            cleave_event = GameEvent(
                event_type="MeleeAttack",
                source_uuid=attacker_ent.entity_uuid,
                target_uuid=cleave_target.entity_uuid,
                vault_path=parent_event.vault_path,
                payload={"cleave_attack": True, "suppress_ability_mod_damage": True},
            )
            EventBus.dispatch(cleave_event)
            attacker_ent.resources["Cleave Used"] = "1/1"
            hit_str = "HIT" if cleave_event.payload.get("hit") else "MISS"
            results_out.append(
                f"[Cleave Mastery Triggered] Extra attack vs {cleave_target.name}: {hit_str}."
            )
            return

        # REQ-MST-003: Nick — alert that the extra Light weapon attack doesn't cost a Bonus Action
        if mastery_type == "nick":
            if attacker_ent.resources.get("Nick Used") == "1/1":
                return
            attacker_ent.resources["Nick Used"] = "1/1"
            results_out.append(
                f"[Nick Mastery Triggered] {attacker_ent.name}'s extra Light weapon attack can be made as part of "
                f"the Attack action (no Bonus Action cost). Once per turn."
            )
            return

        # REQ-MST-004: Push — forced movement straight away from attacker
        if mastery_type == "push":
            # Size check: target must be ≤ one size larger than attacker
            if target_ent.size > attacker_ent.size + 5.0:
                results_out.append(
                    f"[Push Mastery] {target_ent.name} is too large to be pushed by {attacker_ent.name}."
                )
                return
            dx = target_ent.x - attacker_ent.x
            dy = target_ent.y - attacker_ent.y
            dist_2d = math.sqrt(dx * dx + dy * dy)
            if dist_2d > 0:
                nx, ny = dx / dist_2d, dy / dist_2d
            else:
                nx, ny = 1.0, 0.0  # default direction if co-located
            push_dist = mech.get("push_distance", 10.0)
            target_ent.x += nx * push_dist
            target_ent.y += ny * push_dist
            spatial_service.sync_entity(target_ent)
            results_out.append(
                f"[Push Mastery Triggered] {target_ent.name} is pushed {push_dist}ft away from {attacker_ent.name}."
            )
            return

        # REQ-MST-006: Slow — apply Slowed condition reducing speed by 10ft
        if mastery_type == "slow":
            already_slowed = any(
                c.name.lower() == "slowed" and c.source_uuid == attacker_ent.entity_uuid
                for c in target_ent.active_conditions
            )
            if already_slowed:
                results_out.append(f"[Slow Mastery] {target_ent.name} is already Slowed by {attacker_ent.name}.")
                return
            speed_reduction = mech.get("speed_reduction", 10)
            slow_cond = ActiveCondition(
                name="Slowed",
                source_name=f"{attacker_ent.name} (Slow Mastery)",
                source_uuid=attacker_ent.entity_uuid,
                speed_reduction=speed_reduction,
            )
            target_ent.active_conditions.append(slow_cond)
            target_ent.movement_remaining = max(0, target_ent.movement_remaining - speed_reduction)
            results_out.append(
                f"[Slow Mastery Triggered] {target_ent.name}'s speed is reduced by {speed_reduction}ft "
                f"until the start of {attacker_ent.name}'s next turn."
            )
            return

        # Generic mastery: route through SpellCast
        if mech.get("damage_dice") == "ability_mod":
            mech["damage_dice"] = str(mod_val)
        if mech.get("damage_type") == "weapon":
            mech["damage_type"] = wpn.damage_type
        # Mastery save DC = 8 + weapon attack bonus (= 8 + prof_bonus + ability_mod)
        mastery_save_dc = 8 + mod_val

        mastery_event = GameEvent(
            event_type="SpellCast",
            source_uuid=attacker_ent.entity_uuid,
            vault_path=parent_event.vault_path,
            payload={
                "ability_name": f"{wpn.mastery_name} Mastery",
                "mechanics": mech,
                "target_uuids": [target_ent.entity_uuid],
                "save_dc_override": mastery_save_dc,
            },
        )
        res = EventBus.dispatch(mastery_event)
        if "results" in res.payload and res.payload["results"]:
            results_out.append(
                f"[{wpn.mastery_name} Mastery Triggered] " + " ".join(res.payload["results"])
            )

    # Don't trigger masteries on secondary mastery attacks (e.g., Cleave's extra attack)
    if not event.payload.get("cleave_attack"):
        if is_hit and weapon.on_hit_mechanics:
            _trigger_weapon_mastery(attacker, target, weapon, weapon.on_hit_mechanics, event)
        elif not is_hit and weapon.on_miss_mechanics:
            _trigger_weapon_mastery(attacker, target, weapon, weapon.on_miss_mechanics, event)


def apply_damage_handler(event: GameEvent):
    """Applies final damage to a target, considering immunities, resistances, and vulnerabilities."""
    if event.status != EventStatus.EXECUTION:
        return

    target: Creature = get_entity(event.target_uuid)
    if not target:
        return

    damage = event.payload.get("damage", 0)
    damage_type = event.payload.get("damage_type", "unknown")
    is_critical = event.payload.get("critical", False)
    source_name = event.payload.get("source_name")

    results = []

    if damage > 0:
        is_magical_damage = event.payload.get("magical", False)

        # REQ-DMG-005: Compound immunity/resistance entries like "nonmagical slashing" match the base
        # damage type, but magical attacks bypass those entries.
        def _entry_matches(entry: str, dtype: str) -> bool:
            el = entry.lower()
            return el == dtype or el.endswith(" " + dtype)

        def _entry_is_nonmagical_qualified(entry: str) -> bool:
            el = entry.lower()
            return "nonmagical" in el or "non-magical" in el

        def _dtype_in_list(lst, dtype, bypass_nonmagical=False):
            for e in lst:
                if _entry_matches(e, dtype):
                    if bypass_nonmagical and _entry_is_nonmagical_qualified(e):
                        continue  # magical attack bypasses this entry
                    return True
            return False

        # REQ-ENV-006: Submerged entities gain Fire resistance
        is_submerged = any(t.lower() in ["underwater", "submerged"] for t in target.tags) or any(
            c.name.lower() in ["underwater", "submerged"] for c in target.active_conditions
        )
        if is_submerged and damage_type == "fire" and "fire" not in [r.lower() for r in target.resistances]:
            damage = damage // 2
            results.append(f"[Engine] {target.name} is submerged — Fire damage halved (REQ-ENV-006).")

        # Check for immunities first
        is_magically_silenced = any(
            c.name.lower() == "silenced" and "silence" in c.source_name.lower() for c in target.active_conditions
        )

        if _dtype_in_list(target.immunities, damage_type, bypass_nonmagical=is_magical_damage) or (
            damage_type == "thunder" and is_magically_silenced
        ):
            damage = 0
            results.append(f"[Engine] {target.name} is IMMUNE to {damage_type}!")
        else:
            # Then check for vulnerabilities and resistances
            if _dtype_in_list(target.vulnerabilities, damage_type):
                damage *= 2
                results.append(f"[Engine] {target.name} is VULNERABLE to {damage_type}! Damage is doubled.")
            elif _dtype_in_list(target.resistances, damage_type, bypass_nonmagical=is_magical_damage):
                damage = damage // 2  # Halve the damage, rounding down
                results.append(f"[Engine] {target.name} is RESISTANT to {damage_type}! Damage is halved.")

        if damage > 0 and target.temp_hp > 0:
            if damage >= target.temp_hp:
                results.append(f"[Engine] {target.name}'s {target.temp_hp} Temporary HP absorbed some of the damage!")
                damage -= target.temp_hp
                target.temp_hp = 0
            else:
                results.append(f"[Engine] {target.name}'s Temporary HP completely absorbed the damage!")
                target.temp_hp -= damage
                damage = 0

        if damage > 0 and target.wild_shape_hp > 0:
            if damage >= target.wild_shape_hp:
                results.append(f"[Engine] {target.name}'s Wild Shape absorbed some damage, but they revert to normal form!")
                damage -= target.wild_shape_hp
                target.wild_shape_hp = 0
                target.active_conditions = [c for c in target.active_conditions if c.name != "Wild Shape"]
            else:
                results.append(f"[Engine] {target.name}'s Wild Shape absorbed the damage!")
                target.wild_shape_hp -= damage
                damage = 0

        # REQ-CLS-001: Rage maintenance — taking damage counts as a valid rage-sustaining action
        if damage > 0 and any(c.name.lower() == "raging" for c in target.active_conditions):
            target.resources["Raged This Cycle"] = "1/1"

        current_hp = target.hp.base_value
        target.hp.base_value -= damage

        check_death_and_dying(target, current_hp, damage, is_critical, results)

        source_str = f" from {source_name}" if source_name else ""
        results.append(
            f"[Engine] {target.name} took {damage} {damage_type} damage{source_str}. HP remaining: {target.hp.base_value} (THP: {target.temp_hp})"
        )

        if target.hp.base_value <= 0:
            if target.concentrating_on:
                EventBus.dispatch(
                    GameEvent(event_type="DropConcentration", source_uuid=target.entity_uuid, vault_path=event.vault_path)
                )
        elif damage > 0 and target.concentrating_on:
            dc = max(10, damage // 2)
            msg = (
                f"[Engine] SYSTEM ALERT: {target.name} took damage while concentrating on '{target.concentrating_on}'. "
                f"LLM MUST prompt a Constitution saving throw (DC {dc}). Use `drop_concentration` tool if failed."
            )
            results.append(msg)

    for r in results:
        print(r)

    event.payload.setdefault("results", []).extend(results)
    event.payload["final_damage_applied"] = damage


def melee_attack_damage_dispatcher(event: GameEvent):
    """Dispatches an ApplyDamage event after a successful melee attack."""
    if event.status != EventStatus.POST_EVENT or not event.payload.get("hit"):
        return

    damage_event = GameEvent(
        event_type="ApplyDamage",
        source_uuid=event.source_uuid,
        target_uuid=event.target_uuid,
        vault_path=event.vault_path,
        payload={
            "damage": event.payload.get("damage", 0),
            "damage_type": event.payload.get("damage_type", "unknown"),
            "critical": event.payload.get("critical", False)
        }
    )
    EventBus.dispatch(damage_event)
    event.payload.setdefault("results", []).extend(damage_event.payload.get("results", []))


def handle_rest_event(event: GameEvent):
    """Deterministically recharges resources and resets HP on a rest."""
    if event.status != EventStatus.EXECUTION:
        return

    rest_type = event.payload.get("rest_type", "short")
    target_uuids = event.payload.get("target_uuids", [])

    for uid in target_uuids:
        target = get_entity(uid)
        if not isinstance(target, Creature):
            continue

        if rest_type == "long":
            target.hp.base_value = target.max_hp
            for res_name, res_val in target.resources.items():
                match = re.match(r"(\d+)\s*/\s*(\d+)", str(res_val))
                if match:
                    current = int(match.group(1))
                    maximum = int(match.group(2))
                    if "hit dice" in res_name.lower():
                        recover = max(1, maximum // 2)
                        new_val = min(maximum, current + recover)
                        target.resources[res_name] = f"{new_val}/{maximum}"
                    else:
                        target.resources[res_name] = f"{maximum}/{maximum}"
            print(f"[Engine] {target.name} finished a Long Rest. HP and resources fully restored.")

        elif rest_type == "short":
            # REQ-CLS-008/011: Reset resources tagged [SR] (Short Rest reset)
            for res_name in list(target.resources.keys()):
                if "[SR]" in res_name:
                    match = re.match(r"(\d+)\s*/\s*(\d+)", str(target.resources[res_name]))
                    if match:
                        maximum = int(match.group(2))
                        target.resources[res_name] = f"{maximum}/{maximum}"

            # REQ-RST-001: Spend Hit Dice to regain HP during a Short Rest
            dice_to_spend = event.payload.get("hit_dice_to_spend", 0)
            if dice_to_spend > 0:
                # Find the Hit Dice resource (key contains "hit dice", case-insensitive)
                hd_key = next((k for k in target.resources if "hit dice" in k.lower()), None)
                if hd_key:
                    hd_match = re.match(r"(\d+)\s*/\s*(\d+)", str(target.resources[hd_key]))
                    if hd_match:
                        hd_current = int(hd_match.group(1))
                        hd_max = int(hd_match.group(2))
                        dice_available = min(dice_to_spend, hd_current)
                        if dice_available > 0:
                            # Determine die size from resource key, e.g. "Hit Dice (d8)" → "1d8"
                            die_match = re.search(r"d(\d+)", hd_key, re.IGNORECASE)
                            die_str = f"1d{die_match.group(1)}" if die_match else "1d8"
                            con_mod = target.constitution_mod.total if hasattr(target, "constitution_mod") else 0
                            total_heal = 0
                            for _ in range(dice_available):
                                total_heal += max(1, roll_dice(die_str) + con_mod)
                            old_hp = target.hp.base_value
                            target.hp.base_value = min(target.max_hp, old_hp + total_heal)
                            target.resources[hd_key] = f"{hd_current - dice_available}/{hd_max}"
                            print(
                                f"[Engine] {target.name} spent {dice_available}x {die_str} Hit Dice "
                                f"(+{con_mod} CON each). Healed {total_heal} HP "
                                f"({old_hp} → {target.hp.base_value}). "
                                f"Hit Dice remaining: {hd_current - dice_available}/{hd_max}."
                            )

            print(f"[Engine] {target.name} finished a Short Rest. [SR] resources restored.")


def handle_advance_time_event(event: GameEvent):  # noqa: C901
    """Deterministically expires temporary modifiers when time advances."""
    if event.status != EventStatus.EXECUTION:
        return

    seconds_advanced = event.payload.get("seconds_advanced", 0)
    target_init = event.payload.get("target_initiative", None)
    if seconds_advanced <= 0:
        return

    for uid, entity in get_all_entities(event.vault_path).items():
        if isinstance(entity, Creature):
            # Check all ModifiableValue attributes for temporary modifiers
            for field_name in type(entity).model_fields:
                stat_val = getattr(entity, field_name)
                if isinstance(stat_val, ModifiableValue):
                    expired_mods = []
                    for mod in stat_val.modifiers:
                        if mod.duration_seconds > 0:
                            if target_init is None or mod.applied_initiative == target_init:
                                mod.duration_seconds -= seconds_advanced
                                if mod.duration_seconds <= 0:
                                    expired_mods.append(mod)

                    for mod in expired_mods:
                        stat_val.remove_modifier(mod.mod_uuid)
                        if mod.source_name in entity.active_mechanics:
                            entity.active_mechanics.remove(mod.source_name)
                        print(
                            f"[Engine] {entity.name}'s temporary mechanic '{mod.source_name}' on '{field_name}' has expired."
                        )

            # Clean up active conditions
            expired_conditions = []
            for cond in entity.active_conditions:
                if cond.duration_seconds > 0:
                    if target_init is None or cond.applied_initiative == target_init:
                        cond.duration_seconds -= seconds_advanced
                        if cond.duration_seconds <= 0:
                            expired_conditions.append(cond)
            for cond in expired_conditions:
                entity.active_conditions.remove(cond)
                print(f"[Engine] {entity.name} is no longer {cond.name}.")

    # Expire temporary terrain
    expired_terrains = []
    for tz in spatial_service.get_map_data(event.vault_path).temporary_terrain:
        if getattr(tz, "duration_seconds", -1) > 0:
            if target_init is None or getattr(tz, "applied_initiative", 0) == target_init:
                tz.duration_seconds -= seconds_advanced
                if tz.duration_seconds <= 0:
                    expired_terrains.append(tz)
    for tz in expired_terrains:
        spatial_service.remove_terrain(tz.zone_id, event.vault_path)
        print(f"[Engine] Temporary terrain '{tz.label}' has expired and faded away.")


def deflect_attacks_reaction_handler(event: GameEvent):
    """REQ-CLS-012: Monk Deflect Attacks — reduces weapon hit damage as a Reaction, POST_EVENT."""
    if event.status != EventStatus.POST_EVENT:
        return
    if not event.payload.get("hit"):
        return

    target: Creature = get_entity(event.target_uuid)
    if not target or "deflect_attacks" not in target.tags:
        return
    if target.reaction_used:
        return

    # Compute reduction: 1d10 + dex_mod + monk_level
    monk_level = next((c.level for c in target.classes if c.class_name.lower() == "monk"), 0)
    reduction = roll_dice("1d10") + target.dexterity_mod.total + monk_level

    current_damage = event.payload.get("damage", 0)
    reduced_damage = max(0, current_damage - reduction)
    target.reaction_used = True
    event.payload["damage"] = reduced_damage

    msg = (
        f"[Engine] {target.name} uses Deflect Attacks! Reduced incoming damage by {reduction} "
        f"({current_damage} → {reduced_damage}). (REQ-CLS-012)"
    )
    print(msg)
    event.payload.setdefault("results", []).append(msg)

    if reduced_damage == 0:
        # Damage fully negated; can redirect by spending 1 Focus Point
        focus_val = target.resources.get("Focus Points [SR]", "")
        m = re.match(r"(\d+)/(\d+)", str(focus_val))
        if m and int(m.group(1)) >= 1:
            event.payload.setdefault("results", []).append(
                f"[Engine] SYSTEM ALERT: {target.name} fully deflected the attack — may spend 1 Focus Point "
                f"to redirect the projectile. Call use_ability_or_spell('Deflect Attacks Redirect') to apply."
            )


def shield_spell_reaction_handler(event: GameEvent):
    """Intercepts an attack BEFORE it resolves and magically raises AC."""
    if event.status != EventStatus.PRE_EVENT:
        return

    target_uuids = []
    if event.target_uuid:
        target_uuids.append(event.target_uuid)
    elif "target_uuids" in event.payload:
        target_uuids.extend(event.payload["target_uuids"])

    current_init = event.payload.get("current_initiative", 0)

    for t_uuid in target_uuids:
        target: Creature = get_entity(t_uuid)
        if target and "can_cast_shield" in target.tags and not target.reaction_used:
            print(f"[Engine] REACTION TRIGGERED: {target.name} casts Shield!")
            target.reaction_used = True
            shield_mod = NumericalModifier(
                priority=ModifierPriority.ADDITIVE,
                value=5,
                source_name="Shield Spell",
                duration_seconds=6,  # Shield lasts 1 round
                applied_initiative=current_init,
            )
            target.ac.add_modifier(shield_mod)
            if "Shield Spell" not in target.active_mechanics:
                target.active_mechanics.append("Shield Spell")
            print(f"[Engine] {target.name}'s AC is temporarily raised to {target.ac.total}")


def handle_drop_concentration_event(event: GameEvent):
    """Removes all modifiers and conditions applied by a concentrated spell."""
    if event.status != EventStatus.EXECUTION:
        return

    caster: Creature = get_entity(event.source_uuid)
    if not caster or not caster.concentrating_on:
        return

    spell_name = caster.concentrating_on
    print(f"[Engine] {caster.name} lost concentration on {spell_name}.")

    caster.concentrating_on = ""

    for uid, entity in get_all_entities(event.vault_path).items():
        if isinstance(entity, Creature):
            for field_name in type(entity).model_fields:
                stat_val = getattr(entity, field_name)
                if isinstance(stat_val, ModifiableValue):
                    expired_mods = [
                        m
                        for m in stat_val.modifiers
                        if m.source_name == spell_name and (m.source_uuid == caster.entity_uuid or m.source_uuid is None)
                    ]
                    for mod in expired_mods:
                        stat_val.remove_modifier(mod.mod_uuid)
                        if mod.source_name in entity.active_mechanics:
                            entity.active_mechanics.remove(mod.source_name)
                        print(f"[Engine] {entity.name}'s {spell_name} modifier on '{field_name}' faded.")

            expired_conditions = [
                c
                for c in entity.active_conditions
                if c.source_name == spell_name and (c.source_uuid == caster.entity_uuid or c.source_uuid is None)
            ]
            for cond in expired_conditions:
                entity.active_conditions.remove(cond)
                print(f"[Engine] {entity.name} is no longer {cond.name} from {spell_name}.")

    # Remove terrain tied to this caster's concentration spell
    expired_terrains = [
        tz
        for tz in spatial_service.get_map_data(event.vault_path).temporary_terrain
        if getattr(tz, "source_uuid", None) == caster.entity_uuid and getattr(tz, "source_name", "") == spell_name
    ]
    for tz in expired_terrains:
        spatial_service.remove_terrain(tz.zone_id, event.vault_path)
        print(f"[Engine] Temporary terrain '{tz.label}' dissipated as {caster.name} dropped concentration.")

    despawned_entities = []
    for uid, entity in get_all_entities(event.vault_path).items():
        if isinstance(entity, Creature):
            if entity.summoned_by_uuid == caster.entity_uuid and entity.summon_spell == spell_name:
                despawned_entities.append(entity)

    for ent in despawned_entities:
        ent.hp.base_value = 0
        if not any(c.name == "Dead" for c in ent.active_conditions):
            ent.active_conditions.append(ActiveCondition(name="Dead", source_name="Concentration Dropped"))
        print(f"[Engine] {ent.name} despawned (concentration dropped).")


def validate_movement_handler(event: GameEvent):
    """Validates movement bounds, speed limits, and difficult terrain costs."""
    if event.status != EventStatus.PRE_EVENT:
        return
    movement_type = event.payload.get("movement_type", "walk").lower()

    # Teleport, fall, forced don't consume standard movement points
    if movement_type in ["teleport", "forced", "fall", "travel"]:
        return

    entity = get_entity(event.source_uuid)
    if not isinstance(entity, Creature):
        return

    # 1. Enforce Condition-Based Restrictions
    active_conds = [c.name.lower() for c in entity.active_conditions]
    zero_speed_conds = {"grappled", "restrained", "stunned", "paralyzed", "petrified", "unconscious"}
    if any(cond in active_conds for cond in zero_speed_conds):
        event.status = EventStatus.CANCELLED
        event.payload["error"] = f"Movement failed. {entity.name} is suffering from a condition that reduces their speed to 0."
        return

    target_x = event.payload.get("target_x")
    target_y = event.payload.get("target_y")
    target_z = event.payload.get("target_z", entity.z)

    normal_dist, diff_dist = spatial_service.calculate_path_terrain_costs(
        entity.x, entity.y, entity.z, target_x, target_y, target_z, event.vault_path
    )

    # Evaluate Character Traits/Feats/Items
    if "ignore_difficult_terrain" in entity.tags:
        normal_dist += diff_dist
        diff_dist = 0.0

    cost_multiplier = 1
    if "squeezing" in active_conds:
        cost_multiplier += 1
    # REQ-MOV-005: Climbing/swimming costs 1 extra foot UNLESS the entity has a native speed for it
    if movement_type == "climb" and "climb_speed" not in entity.tags:
        cost_multiplier += 1
    elif movement_type == "swim" and "swim_speed" not in entity.tags:
        cost_multiplier += 1
    elif movement_type == "crawl":
        cost_multiplier += 1

    if event.payload.get("dragged_uuids"):
        cost_multiplier += 1  # Dragging halves speed (costs 1 extra foot per foot)

    raw_dist = (normal_dist * cost_multiplier) + (diff_dist * (cost_multiplier + 1))
    total_cost = math.ceil(int(raw_dist * 100) / 100.0)

    if "prone" in active_conds and movement_type == "walk":
        total_cost += math.floor(entity.speed / 2)

    if total_cost > entity.movement_remaining and not event.payload.get("ignore_budget", False):
        event.status = EventStatus.CANCELLED
        event.payload["error"] = (
            f"Movement cost ({total_cost}ft) exceeds remaining speed ({entity.movement_remaining}ft). "
            f"Normal dist: {normal_dist:.1f}ft, Difficult dist: {diff_dist:.1f}ft."
        )
        return

    event.payload["cost"] = total_cost


def resolve_movement_handler(event: GameEvent):  # noqa: C901
    """Evaluates movement to see if it provokes opportunity attacks."""
    if event.status != EventStatus.EXECUTION:
        return

    movement_type = event.payload.get("movement_type", "walk").lower()

    entity: Creature = get_entity(event.source_uuid)
    target_x = event.payload.get("target_x")
    target_y = event.payload.get("target_y")
    target_z = event.payload.get("target_z", entity.z)

    # --- Prone Recovery ---
    if movement_type == "walk":
        prone_conds = [c for c in entity.active_conditions if c.name.lower() == "prone"]
        for cond in prone_conds:
            entity.active_conditions.remove(cond)
            print(f"[Engine] {entity.name} stood up from Prone.")

    # --- Grapple Breaking Checks ---
    # 1. Did the moving entity break out of a grapple? (e.g. Thunderwaved away)
    grappled_conds = [c for c in entity.active_conditions if c.name.lower() == "grappled" and c.source_uuid]
    for cond in grappled_conds:
        grappler = get_entity(cond.source_uuid)
        if grappler:
            dist_after = spatial_service.calculate_distance(
                grappler.x, grappler.y, grappler.z, target_x, target_y, target_z, event.vault_path
            )
            if dist_after > 7.5:
                entity.active_conditions.remove(cond)
                print(f"[Engine] {entity.name} was moved out of {grappler.name}'s reach. The grapple is broken!")

    # 2. Did the moving entity abandon a grapple they were maintaining? (e.g. Teleporting away without dragging)
    dragged_uuids = event.payload.get("dragged_uuids", [])
    for uid, other_ent in get_all_entities(event.vault_path).items():
        if isinstance(other_ent, Creature) and uid not in dragged_uuids:
            for cond in other_ent.active_conditions:
                if cond.name.lower() == "grappled" and cond.source_uuid == entity.entity_uuid:
                    dist_after = spatial_service.calculate_distance(
                        target_x, target_y, target_z, other_ent.x, other_ent.y, other_ent.z, event.vault_path
                    )
                    if dist_after > 7.5:
                        other_ent.active_conditions.remove(cond)
                        print(f"[Engine] {entity.name} moved away from {other_ent.name}. The grapple is broken!")

    # Teleportation and forced movement do not provoke opportunity attacks natively
    if movement_type in ["teleport", "forced", "travel"]:
        return

    opportunity_attackers = []
    for uid, potential_attacker in get_all_entities(event.vault_path).items():
        if uid == entity.entity_uuid:
            continue
        if not isinstance(potential_attacker, Creature):
            continue
        if potential_attacker.hp.base_value <= 0:
            continue

        has_reaction = not potential_attacker.reaction_used
        has_legendary = potential_attacker.legendary_actions_current > 0
        if not has_reaction and not has_legendary:
            continue

        # Check hostility
        is_entity_pc = any(t in entity.tags for t in ["pc", "player", "party_npc"])
        is_attacker_pc = any(t in potential_attacker.tags for t in ["pc", "player", "party_npc"])
        if is_entity_pc == is_attacker_pc:
            continue

        # Disengage Check
        if movement_type == "disengage" and "ignores_disengage" not in potential_attacker.tags:
            continue

        # Reach
        reach = 5.0
        if potential_attacker.equipped_weapon_uuid:
            weapon = get_entity(potential_attacker.equipped_weapon_uuid)
            if hasattr(weapon, "properties") and WeaponProperty.REACH in weapon.properties:
                reach = 10.0
        reach *= 1.5  # Diagonal allowance

        dist_before = spatial_service.calculate_distance(
            potential_attacker.x, potential_attacker.y, potential_attacker.z, entity.x, entity.y, entity.z, event.vault_path
        )
        dist_after = spatial_service.calculate_distance(
            potential_attacker.x, potential_attacker.y, potential_attacker.z, target_x, target_y, target_z, event.vault_path
        )

        if dist_before <= reach and dist_after > reach:
            opportunity_attackers.append(potential_attacker.name)

    if opportunity_attackers:
        event.payload["opportunity_attackers"] = opportunity_attackers

    # --- Reveal Fog of War ---
    if any(t in entity.tags for t in ["pc", "player"]):
        # Determine max vision range
        vision_radius = 30.0  # Default bright light assumed
        for t in entity.tags:
            if "darkvision" in t:
                try:
                    vision_radius = max(vision_radius, float(t.split("_")[1]))
                except Exception:
                    vision_radius = max(vision_radius, 60.0)

        # Only reveal what isn't blocked by walls (simplified to a radius for now,
        # true raycast FoW clearing is handled client-side in the Canvas)
        spatial_service.reveal_fog_of_war(target_x, target_y, vision_radius, event.vault_path)


def consume_movement_handler(event: GameEvent):
    """Deducts movement speed after a successful, un-cancelled move."""
    if event.status != EventStatus.POST_EVENT:
        return
    entity = get_entity(event.source_uuid)
    if isinstance(entity, Creature) and "cost" in event.payload:
        if not event.payload.get("ignore_budget", False):
            entity.movement_remaining -= event.payload["cost"]


def trap_movement_handler(event: GameEvent):
    """Evaluates movement to see if it intersects with trapped geometry."""
    if event.status != EventStatus.EXECUTION:
        return

    movement_type = event.payload.get("movement_type", "walk").lower()
    if movement_type in ["teleport", "forced", "travel"]:
        return

    entity = get_entity(event.source_uuid)
    if not isinstance(entity, Creature):
        return

    target_x = event.payload.get("target_x")
    target_y = event.payload.get("target_y")

    if not HAS_GIS:
        return
    from shapely.geometry import LineString

    path = LineString([(entity.x, entity.y), (target_x, target_y)])
    traps_triggered = []

    for wall in spatial_service.get_map_data(event.vault_path).active_walls:
        if wall.trap and wall.trap.is_active and wall.trap.trigger_on_move and path.intersects(wall.line):
            traps_triggered.append((wall.trap, target_x, target_y))

    for zone in spatial_service.get_map_data(event.vault_path).active_terrain:
        if zone.trap and zone.trap.is_active and zone.trap.trigger_on_move and path.intersects(zone.polygon):
            traps_triggered.append((zone.trap, target_x, target_y))

    for trap, ox, oy in traps_triggered:
        if not trap.is_active:
            continue
        if not getattr(trap, "is_persistent", False):
            trap.is_active = False  # Deactivate standard traps after triggering once
        print(f"[Engine] TRAP TRIGGERED: {trap.hazard_name} during movement!")

        target_uuids = {entity.entity_uuid}
        if trap.radius > 0:
            spatial_hits = spatial_service.get_targets_in_radius(ox, oy, trap.radius, event.vault_path)
            target_uuids.update(spatial_hits)

        trap_source = Creature(
            name=trap.hazard_name,
            vault_path=event.vault_path,
            tags=["trap"],
            hp=ModifiableValue(base_value=1),
            ac=ModifiableValue(base_value=10),
            spell_save_dc=ModifiableValue(base_value=trap.save_dc),
            spell_attack_bonus=ModifiableValue(base_value=trap.attack_bonus),
            strength_mod=ModifiableValue(base_value=0),
            dexterity_mod=ModifiableValue(base_value=0),
        )

        mechanics = {
            "requires_attack_roll": trap.requires_attack_roll,
            "save_required": trap.save_required,
            "damage_dice": trap.damage_dice,
            "damage_type": trap.damage_type,
            "half_damage_on_save": trap.half_damage_on_save,
            "conditions_applied": (
                [{"condition": trap.condition_applied, "duration": "1 minute"}] if trap.condition_applied else []
            ),
        }

        trap_event = GameEvent(
            event_type="SpellCast",
            source_uuid=trap_source.entity_uuid,
            payload={"ability_name": trap.hazard_name, "mechanics": mechanics, "target_uuids": list(target_uuids)},
        )
        trap_result = EventBus.dispatch(trap_event)
        BaseGameEntity.remove(trap_source.entity_uuid)

        if "trap_results" not in event.payload:
            event.payload["trap_results"] = []
        event.payload["trap_results"].extend(trap_result.payload.get("results", []))


def trap_noise_handler(event: GameEvent):
    """Automatically tests PC stealth against global NPC passive perception when a trap is triggered."""
    if event.status != EventStatus.POST_EVENT:
        return

    caster = get_entity(event.source_uuid)
    if not caster or "trap" not in caster.tags:
        return

    target_uuids = event.payload.get("target_uuids", [])
    triggering_pc = get_entity(target_uuids[0]) if target_uuids else None

    stealth_score = 10
    if isinstance(triggering_pc, Creature):
        stealth_score = 10 + triggering_pc.dexterity_mod.total
        # Remove Hidden status from the PC who blundered into the trap
        hidden_conds = [c for c in triggering_pc.active_conditions if c.name.lower() == "hidden"]
        if hidden_conds:
            for c in hidden_conds:
                triggering_pc.active_conditions.remove(c)
            if "results" not in event.payload:
                event.payload["results"] = []
            event.payload["results"].append(f"[{triggering_pc.name}] lost their 'Hidden' status from triggering the trap.")

    alerted_npcs = []
    for uid, ent in get_all_entities(event.vault_path).items():
        if isinstance(ent, Creature) and ent.hp.base_value > 0:
            is_pc = any(t in ent.tags for t in ["pc", "player", "party_npc"])
            if is_pc:
                continue

            dist = 0
            if triggering_pc and HAS_GIS:
                dist = spatial_service.calculate_distance(
                    ent.x, ent.y, ent.z, triggering_pc.x, triggering_pc.y, triggering_pc.z, event.vault_path
                )

            # -1 penalty to passive perception for every 10 ft of distance
            distance_penalty = int(dist // 10)
            passive_perception = 10 + ent.wisdom_mod.total - distance_penalty

            if passive_perception >= stealth_score:
                alerted_npcs.append(ent.name)

    if alerted_npcs:
        msg = (
            f"SYSTEM ALERT: The trap generated noise! The following NPCs beat the triggering entity's "
            f"Passive Stealth (DC {stealth_score}) with their Passive Perception and are now ALERTED: "
            f"{', '.join(alerted_npcs)}."
        )
        if "results" not in event.payload:
            event.payload["results"] = []
        event.payload["results"].append(msg)


def evasion_save_handler(event: GameEvent):
    """Intercepts dexterity saving throws to apply the Evasion feat mechanics."""
    if event.status != EventStatus.EXECUTION:
        return

    target: Creature = get_entity(event.target_uuid)
    if not target or "evasion" not in target.tags:
        return

    if event.payload.get("save_required") == "dexterity":
        print(f"[Engine] {target.name} uses EVASION!")
        is_success = event.payload.get("is_success", False)
        base_damage = event.payload.get("base_damage", 0)

        if is_success:
            event.payload["final_damage"] = 0
            print(f"[Engine] {target.name} dodges completely, taking 0 damage.")
        else:
            event.payload["final_damage"] = base_damage // 2
            print(f"[Engine] {target.name} fails the save, but Evasion reduces it to half damage.")


def counterspell_reaction_handler(event: GameEvent):  # noqa: C901
    """Intercepts a spell cast and attempts to counter it (REQ-SPL-015)."""
    if event.status != EventStatus.PRE_EVENT:
        return

    caster: Creature = get_entity(event.source_uuid)
    if not caster:
        return

    is_caster_pc = any(t in caster.tags for t in ["pc", "player", "party_npc"])

    for uid, pot_counterspeller in get_all_entities(event.vault_path).items():
        if uid == caster.entity_uuid:
            continue
        if not isinstance(pot_counterspeller, Creature) or pot_counterspeller.hp.base_value <= 0:
            continue

        if "can_cast_counterspell" not in pot_counterspeller.tags:
            continue

        if pot_counterspeller.reaction_used:
            continue

        is_counterspeller_pc = any(t in pot_counterspeller.tags for t in ["pc", "player", "party_npc"])
        if is_caster_pc == is_counterspeller_pc:
            continue

        if HAS_GIS:
            dist = spatial_service.calculate_distance(
                pot_counterspeller.x,
                pot_counterspeller.y,
                pot_counterspeller.z,
                caster.x,
                caster.y,
                caster.z,
                event.vault_path,
            )
            if dist > 60.0 or not spatial_service.has_line_of_sight(
                pot_counterspeller.entity_uuid, caster.entity_uuid, event.vault_path
            ):
                continue

        print(f"[Engine] REACTION TRIGGERED: {pot_counterspeller.name} casts Counterspell!")
        pot_counterspeller.reaction_used = True

        save_mod = caster.constitution_mod.total
        save_roll = random.randint(1, 20)
        total_save = save_roll + save_mod
        dc = pot_counterspeller.spell_save_dc.total

        if total_save < dc:
            event.status = EventStatus.CANCELLED
            msg = f"[Engine] {caster.name} failed CON save ({total_save} vs DC {dc}). The spell fails, but the spell slot is preserved."
            
            # In 5e, beginning to cast a new concentration spell instantly breaks the old one, even if countered.
            if event.payload.get("mechanics", {}).get("requires_concentration", False) and caster.concentrating_on:
                EventBus.dispatch(GameEvent(event_type="DropConcentration", source_uuid=caster.entity_uuid, vault_path=event.vault_path))

            print(msg)
            if "results" not in event.payload:
                event.payload["results"] = []
            event.payload["results"].append(msg)
        else:
            msg = f"[Engine] {caster.name} succeeded CON save ({total_save} vs DC {dc}). The Counterspell fails."
            print(msg)
            if "results" not in event.payload:
                event.payload["results"] = []
            event.payload["results"].append(msg)
        return  # Only one counterspell attempt allowed per spell


def dispel_magic_handler(event: GameEvent):  # noqa: C901
    """Terminates magical effects. Higher levels require a check (REQ-SPL-016)."""
    if event.status != EventStatus.EXECUTION:
        return
    if event.payload.get("ability_name", "").lower() != "dispel magic":
        return

    caster: Creature = get_entity(event.source_uuid)
    target_uuids = event.payload.get("target_uuids", [])

    spell_mod = max(caster.intelligence_mod.total, caster.wisdom_mod.total, caster.charisma_mod.total)

    if "results" not in event.payload:
        event.payload["results"] = []

    for t_uid in target_uuids:
        target = get_entity(t_uid)
        if not isinstance(target, Creature):
            continue

        # 1. Dispel Conditions
        conds_to_remove = []
        for cond in target.active_conditions:
            if cond.duration_seconds > 0:
                # Proxy: Effects > 1 hour are mapped to high-level spell tiers (DC 14+)
                spell_level = 4 if cond.duration_seconds > 3600 else 3
                if spell_level <= 3:
                    conds_to_remove.append(cond)
                    event.payload["results"].append(f"[Engine] {cond.name} on {target.name} was instantly dispelled!")
                else:
                    roll = random.randint(1, 20)
                    total = roll + spell_mod
                    dc = 10 + spell_level
                    if total >= dc:
                        conds_to_remove.append(cond)
                        event.payload["results"].append(
                            f"[Engine] Ability check {total} vs DC {dc} SUCCESS. {cond.name} on {target.name} was dispelled!"
                        )
                    else:
                        event.payload["results"].append(
                            f"[Engine] Ability check {total} vs DC {dc} FAILED. {cond.name} on {target.name} remains."
                        )

        for c in conds_to_remove:
            target.active_conditions.remove(c)

        # 2. Dispel Modifiers
        for field_name in type(target).model_fields:
            stat_val = getattr(target, field_name)
            if isinstance(stat_val, ModifiableValue):
                mods_to_remove = []
                for mod in stat_val.modifiers:
                    if mod.duration_seconds > 0:
                        spell_level = 4 if mod.duration_seconds > 3600 else 3
                        if spell_level <= 3:
                            mods_to_remove.append(mod)
                        else:
                            roll = random.randint(1, 20)
                            if roll + spell_mod >= 10 + spell_level:
                                mods_to_remove.append(mod)
                for m in mods_to_remove:
                    stat_val.remove_modifier(m.mod_uuid)
                    if m.source_name in target.active_mechanics:
                        target.active_mechanics.remove(m.source_name)


def terrain_condition_sync_handler(event: GameEvent):
    """Synchronizes dynamic conditions (like Deafened in Silence) based on TerrainZone intersections."""
    if event.status != EventStatus.RESOLVED:
        return

    # Only run on events that change terrain, positions, or time
    if event.event_type not in ["Movement", "SpellCast", "AdvanceTime", "DropConcentration"]:
        return

    if not HAS_GIS:
        return

    for uid, ent in get_all_entities(event.vault_path).items():
        if not isinstance(ent, Creature) or getattr(ent.hp, "base_value", 0) <= 0:
            continue

        ent_poly = spatial_service._get_entity_bbox(ent)
        if not ent_poly:
            continue

        in_silence = False
        for tz in spatial_service.get_map_data(event.vault_path).active_terrain:
            if "silence" in [t.lower() for t in tz.tags] and tz.polygon and tz.polygon.intersects(ent_poly):
                in_silence = True
                break

        # Manage Silenced condition via unique source name to prevent overwriting permanent traits
        silenced_cond = next(
            (c for c in ent.active_conditions if c.name.lower() == "silenced" and c.source_name == "Magical Silence"), None
        )
        if in_silence and not silenced_cond:
            ent.active_conditions.append(ActiveCondition(name="Silenced", source_name="Magical Silence"))
        elif not in_silence and silenced_cond:
            ent.active_conditions.remove(silenced_cond)

        # Manage Deafened condition
        deafened_cond = next(
            (c for c in ent.active_conditions if c.name.lower() == "deafened" and c.source_name == "Magical Silence"), None
        )
        if in_silence and not deafened_cond:
            ent.active_conditions.append(ActiveCondition(name="Deafened", source_name="Magical Silence"))
        elif not in_silence and deafened_cond:
            ent.active_conditions.remove(deafened_cond)


def _evaluate_repeating_saves(entity: Creature, timing: str) -> list:
    """Calculates all repeating saving throws for a specific timing phase (start/end)."""
    results = []
    conditions_to_remove = []

    for cond in entity.active_conditions:
        if cond.save_required and cond.save_dc > 0 and getattr(cond, "save_timing", "end") == timing:
            save_mod = (
                getattr(entity, f"{cond.save_required}_mod").total if hasattr(entity, f"{cond.save_required}_mod") else 0
            )

            auto_fail = False
            active_conds = [c.name.lower() for c in entity.active_conditions]
            if cond.save_required in ["dexterity", "strength"]:
                if any(c in active_conds for c in ["stunned", "paralyzed", "petrified", "unconscious", "incapacitated"]):
                    auto_fail = True

            has_disadv = False
            if cond.save_required == "dexterity" and any(c in active_conds for c in ["restrained", "squeezing"]):
                has_disadv = True

            roll1, roll2 = random.randint(1, 20), random.randint(1, 20)
            save_roll = min(roll1, roll2) if has_disadv else roll1

            if auto_fail:
                save_roll = 1
                total_save = -99
            else:
                total_save = save_roll + save_mod

            if total_save >= cond.save_dc:
                conditions_to_remove.append(cond)
                results.append(
                    f"[Engine] {entity.name} succeeded on their {timing}-of-turn {cond.save_required} save ({total_save} vs DC {cond.save_dc}) and is no longer {cond.name}."
                )
            else:
                results.append(
                    f"[Engine] {entity.name} failed their {timing}-of-turn {cond.save_required} save ({total_save} vs DC {cond.save_dc}) against {cond.name}."
                )

    for cond in conditions_to_remove:
        entity.active_conditions.remove(cond)

    return results


def end_of_turn_save_handler(event: GameEvent):
    """Processes repeating saving throws at the end of a creature's turn."""
    if event.status != EventStatus.EXECUTION:
        return

    entity = get_entity(event.source_uuid)
    if not isinstance(entity, Creature):
        return

    results = []

    for cond in entity.active_conditions:
        # 1. Apply End of Turn Damage First
        if cond.end_of_turn_damage_dice:
            dmg = roll_dice(cond.end_of_turn_damage_dice)
            if dmg > 0:
                damage_event = GameEvent(
                    event_type="ApplyDamage",
                    source_uuid=entity.entity_uuid,
                    target_uuid=entity.entity_uuid,
                    vault_path=event.vault_path,
                    payload={
                        "damage": dmg,
                        "damage_type": cond.end_of_turn_damage_type.lower(),
                        "critical": False,
                        "source_name": cond.name
                    }
                )
                EventBus.dispatch(damage_event)
                results.extend(damage_event.payload.get("results", []))

    # 2. Saving Throws
    save_results = _evaluate_repeating_saves(entity, "end")
    if save_results:
        results.extend(save_results)

    if results:
        event.payload.setdefault("results", []).extend(results)


def start_of_turn_handler(event: GameEvent):
    """Processes start of turn effects like repeating THP and environmental hazards."""
    if event.status != EventStatus.EXECUTION:
        return

    entity = get_entity(event.source_uuid)
    if not isinstance(entity, Creature):
        return

    results = []
    was_already_dying = any(c.name == "Dying" for c in entity.active_conditions)

    # REQ-CLS-001: Rage Maintenance — if still raging but did NOT attack or take damage since last turn, end rage
    if any(c.name.lower() == "raging" for c in entity.active_conditions):
        raged_val = entity.resources.get("Raged This Cycle", "0/1")
        m = re.match(r"(\d+)/(\d+)", str(raged_val))
        if m and int(m.group(1)) == 0:
            entity.active_conditions = [c for c in entity.active_conditions if c.name.lower() != "raging"]
            results.append(
                f"[Engine] {entity.name}'s Rage has ended — no attack or damage taken since last turn. (REQ-CLS-001)"
            )
            print(f"[Engine] {entity.name}'s Rage ended due to inactivity.")
        # Reset the flag for the new turn
        entity.resources["Raged This Cycle"] = "0/1"

    # REQ-EXH-002: Exhaustion level 6 = death
    if entity.exhaustion_level >= 6:
        entity.active_conditions = [c for c in entity.active_conditions if c.name not in ["Dying", "Stable", "Unconscious"]]
        if not any(c.name == "Dead" for c in entity.active_conditions):
            entity.active_conditions.append(ActiveCondition(name="Dead"))
        results.append(f"[Engine] {entity.name} has reached Exhaustion level 6 and is DEAD.")
        print(f"[Engine] {entity.name} died from Exhaustion level 6.")

    # REQ-MST-001/003: Reset Cleave/Nick once-per-turn resources
    for once_key in ("Cleave Used", "Nick Used"):
        if once_key in entity.resources:
            entity.resources[once_key] = "0/1"

    # REQ-MST-006: Slow mastery — expire Slowed conditions THIS entity imposed on others,
    # and apply existing Slowed penalties to this entity's refreshed movement.
    # (a) Clear Slowed conditions on all entities that this entity's weapon imposed
    all_ents = get_all_entities(event.vault_path)
    for other in all_ents.values():
        if not isinstance(other, Creature):
            continue
        other.active_conditions = [
            c for c in other.active_conditions
            if not (c.name.lower() == "slowed" and c.source_uuid == entity.entity_uuid)
        ]
    # (b) Apply any Slowed conditions still on THIS entity to its just-reset movement_remaining
    for cond in entity.active_conditions:
        if cond.name.lower() == "slowed" and cond.speed_reduction > 0:
            entity.movement_remaining = max(0, entity.movement_remaining - cond.speed_reduction)
            results.append(
                f"[Engine] {entity.name} is Slowed — speed reduced by {cond.speed_reduction}ft this turn."
            )

    # REQ-CLS-003: Reset Sneak Attack once-per-turn resource
    if "Sneak Attack" in entity.resources:
        m = re.match(r"(\d+)/(\d+)", str(entity.resources["Sneak Attack"]))
        if m:
            entity.resources["Sneak Attack"] = f"0/{m.group(2)}"

    for cond in entity.active_conditions:
        if cond.start_of_turn_thp > 0:
            if cond.start_of_turn_thp > entity.temp_hp:
                entity.temp_hp = cond.start_of_turn_thp
                results.append(f"[Engine] {entity.name} gained {cond.start_of_turn_thp} Temporary HP from {cond.name}.")

    # REQ-ENV-007/008: Suffocation — track breath hold and choking per creature
    # REQ-ENV-003: Low Oxygen environment (smoke, altitude, etc.) uses same breath hold rules
    is_underwater = any(t.lower() in ["underwater", "submerged"] for t in entity.tags) or any(
        c.name.lower() in ["underwater", "submerged"] for c in entity.active_conditions
    )
    is_low_oxygen = any(c.name.lower() == "low oxygen" for c in entity.active_conditions)
    has_water_breathing = "water_breathing" in entity.tags or any(
        c.name.lower() == "water breathing" for c in entity.active_conditions
    )
    is_construct = "construct" in entity.tags
    if (is_underwater or is_low_oxygen) and not has_water_breathing and not is_construct and entity.hp.base_value > 0:
        con_mod = entity.constitution_mod.total if hasattr(entity, "constitution_mod") else 0
        # Initialize Breath Hold resource if not yet set (max = max(5, (1+CON)*10) rounds)
        if "Breath Hold" not in entity.resources:
            max_hold = max(5, (1 + con_mod) * 10)
            entity.resources["Breath Hold"] = f"{max_hold}/{max_hold}"
        hb_match = re.match(r"(\d+)/(\d+)", str(entity.resources["Breath Hold"]))
        if hb_match:
            hb_current = int(hb_match.group(1))
            hb_max = int(hb_match.group(2))
            if hb_current > 0:
                # Still holding breath — decrement
                entity.resources["Breath Hold"] = f"{hb_current - 1}/{hb_max}"
                results.append(f"[Engine] {entity.name} is holding breath. Breath Hold: {hb_current - 1}/{hb_max} rounds remaining.")
            else:
                # Breath exhausted — transition to choking or continue choking
                max_choke = max(1, 1 + con_mod)
                if "Choking Rounds" not in entity.resources:
                    entity.resources["Choking Rounds"] = f"{max_choke}/{max_choke}"
                    results.append(f"[Engine] {entity.name} has run out of breath! Choking for {max_choke} rounds. (REQ-ENV-007)")
                else:
                    choke_match = re.match(r"(\d+)/(\d+)", str(entity.resources["Choking Rounds"]))
                    if choke_match:
                        choke_current = int(choke_match.group(1))
                        choke_max = int(choke_match.group(2))
                        if choke_current > 0:
                            entity.resources["Choking Rounds"] = f"{choke_current - 1}/{choke_max}"
                            results.append(f"[Engine] {entity.name} is choking! {choke_current - 1} rounds until death. (REQ-ENV-008)")
                        else:
                            # Choking rounds exhausted — entity drops to 0 HP and starts dying
                            entity.hp.base_value = 0
                            if not any(c.name in ["Dying", "Dead"] for c in entity.active_conditions):
                                entity.active_conditions.append(ActiveCondition(name="Dying"))
                                entity.active_conditions.append(ActiveCondition(name="Unconscious"))
                            results.append(f"[Engine] {entity.name} has suffocated! HP dropped to 0. (REQ-ENV-008)")
                            print(f"[Engine] {entity.name} suffocated.")

    # Evaluate Death Saving Throws (only if entity was already dying at start of turn)
    if was_already_dying and any(c.name == "Dying" for c in entity.active_conditions):
        roll = random.randint(1, 20)
        if roll == 1:
            entity.death_saves_failures += 2
            results.append(
                f"[Engine] {entity.name} rolled a 1 on their Death Save! They suffer 2 failures ({entity.death_saves_failures}/3)."
            )
        elif roll == 20:
            entity.hp.base_value = 1
            entity.active_conditions = [c for c in entity.active_conditions if c.name not in ["Dying", "Unconscious"]]
            entity.death_saves_successes = 0
            entity.death_saves_failures = 0
            results.append(f"[Engine] {entity.name} rolled a 20 on their Death Save! They regain 1 HP and wake up!")
        elif roll >= 10:
            entity.death_saves_successes += 1
            results.append(f"[Engine] {entity.name} succeeded on their Death Save ({entity.death_saves_successes}/3).")
            if entity.death_saves_successes >= 3:
                entity.active_conditions = [c for c in entity.active_conditions if c.name != "Dying"]
                entity.active_conditions.append(ActiveCondition(name="Stable"))
                entity.death_saves_successes = 0
                entity.death_saves_failures = 0
                results.append(f"[Engine] {entity.name} is now STABLE.")
        else:
            entity.death_saves_failures += 1
            results.append(f"[Engine] {entity.name} failed their Death Save ({entity.death_saves_failures}/3).")

        if entity.death_saves_failures >= 3:
            entity.active_conditions = [c for c in entity.active_conditions if c.name not in ["Dying", "Stable"]]
            if not any(c.name == "Dead" for c in entity.active_conditions):
                entity.active_conditions.append(ActiveCondition(name="Dead"))
            results.append(f"[Engine] {entity.name} is DEAD.")

    # Evaluate Spatial Traps (e.g., Flaming Sphere, Moonbeam)
    if HAS_GIS:
        ent_poly = spatial_service._get_entity_bbox(entity)
        if ent_poly:
            traps_triggered = []
            for tz in spatial_service.get_map_data(event.vault_path).active_terrain:
                if getattr(tz, "trap", None) and tz.trap.is_active and getattr(tz.trap, "trigger_on_turn_start", False):
                    if tz.polygon and tz.polygon.intersects(ent_poly):
                        traps_triggered.append(tz.trap)

            for w in spatial_service.get_map_data(event.vault_path).active_walls:
                if getattr(w, "trap", None) and w.trap.is_active and getattr(w.trap, "trigger_on_turn_start", False):
                    if w.line and w.line.intersects(ent_poly):
                        traps_triggered.append(w.trap)

            for trap in traps_triggered:
                if not getattr(trap, "is_persistent", False):
                    trap.is_active = False

                print(f"[Engine] HAZARD TRIGGERED: {trap.hazard_name} at start of {entity.name}'s turn!")

                trap_source = Creature(
                    name=trap.hazard_name,
                    vault_path=event.vault_path,
                    tags=["trap"],
                    hp=ModifiableValue(base_value=1),
                    ac=ModifiableValue(base_value=10),
                    spell_save_dc=ModifiableValue(base_value=trap.save_dc),
                    spell_attack_bonus=ModifiableValue(base_value=trap.attack_bonus),
                    strength_mod=ModifiableValue(base_value=0),
                    dexterity_mod=ModifiableValue(base_value=0),
                )

                mechanics = {
                    "requires_attack_roll": trap.requires_attack_roll,
                    "save_required": trap.save_required,
                    "damage_dice": trap.damage_dice,
                    "damage_type": trap.damage_type,
                    "half_damage_on_save": trap.half_damage_on_save,
                    "conditions_applied": (
                        [{"condition": trap.condition_applied, "duration": "1 minute"}] if trap.condition_applied else []
                    ),
                }

                trap_event = GameEvent(
                    event_type="SpellCast",
                    source_uuid=trap_source.entity_uuid,
                    vault_path=event.vault_path,
                    payload={"ability_name": trap.hazard_name, "mechanics": mechanics, "target_uuids": [entity.entity_uuid]},
                )
                trap_result = EventBus.dispatch(trap_event)
                BaseGameEntity.remove(trap_source.entity_uuid)
                if "results" in trap_result.payload:
                    results.extend(trap_result.payload["results"])

    # Evaluate Start of Turn Saves
    save_results = _evaluate_repeating_saves(entity, "start")
    if save_results:
        results.extend(save_results)

    if results:
        event.payload.setdefault("results", []).extend(results)


def resolve_ability_check_handler(event: GameEvent):
    """Resolves a standard d20 ability check."""
    if event.status != EventStatus.EXECUTION:
        return

    modifier = event.payload.get("modifier", 0)
    dc = event.payload.get("dc", 10)

    entity = get_entity(event.source_uuid)
    exh_penalty = entity.exhaustion_level * 2 if isinstance(entity, Creature) else 0

    # The dice roll is mocked in the test, so random.randint(1, 20) will return the mocked value.
    roll = random.randint(1, 20)
    total_roll = roll + modifier - exh_penalty

    event.payload["roll"] = total_roll
    event.payload["is_success"] = total_roll >= dc


def register_core_handlers():
    """Registers all standard handlers to the Event Bus. Can be called to reset state."""
    EventBus._listeners.clear()
    EventBus.subscribe("AbilityCheck", resolve_ability_check_handler, priority=10)
    EventBus.subscribe("MeleeAttack", shield_spell_reaction_handler, priority=1)
    EventBus.subscribe("MeleeAttack", resolve_attack_handler, priority=10)
    EventBus.subscribe("MeleeAttack", deflect_attacks_reaction_handler, priority=50)
    EventBus.subscribe("MeleeAttack", melee_attack_damage_dispatcher, priority=100)
    EventBus.subscribe("ApplyDamage", apply_damage_handler, priority=10)
    EventBus.subscribe("SpellCast", wild_shape_spellblock_handler, priority=1)
    EventBus.subscribe("SpellCast", shield_spell_reaction_handler, priority=1)
    EventBus.subscribe("SpellCast", counterspell_reaction_handler, priority=1)
    EventBus.subscribe("SpellCast", resolve_spell_cast_handler, priority=10)
    EventBus.subscribe("SpellCast", dispel_magic_handler, priority=15)
    EventBus.subscribe("SpellCast", trap_noise_handler, priority=100)
    EventBus.subscribe("SavingThrow", evasion_save_handler, priority=10)
    EventBus.subscribe("Rest", handle_rest_event, priority=10)
    EventBus.subscribe("AdvanceTime", handle_advance_time_event, priority=10)
    EventBus.subscribe("DropConcentration", handle_drop_concentration_event, priority=10)
    EventBus.subscribe("Movement", validate_movement_handler, priority=5)
    EventBus.subscribe("Movement", resolve_movement_handler, priority=10)
    EventBus.subscribe("Movement", trap_movement_handler, priority=15)
    EventBus.subscribe("Movement", consume_movement_handler, priority=100)
    EventBus.subscribe("Movement", terrain_condition_sync_handler, priority=200)
    EventBus.subscribe("SpellCast", terrain_condition_sync_handler, priority=200)
    EventBus.subscribe("AdvanceTime", terrain_condition_sync_handler, priority=200)
    EventBus.subscribe("DropConcentration", terrain_condition_sync_handler, priority=200)
    EventBus.subscribe("EndOfTurn", end_of_turn_save_handler, priority=10)
    EventBus.subscribe("StartOfTurn", start_of_turn_handler, priority=10)


# Register handlers automatically when imported
register_core_handlers()

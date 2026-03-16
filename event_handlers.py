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


def resolve_spell_cast_handler(event: GameEvent):
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
            EventBus.dispatch(GameEvent(event_type="DropConcentration", source_uuid=caster.entity_uuid))
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
            if save_required == "dexterity" and "restrained" in active_conds:
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
                total_save = save_roll + save_mod_val
            dc = caster.spell_save_dc.total

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

        # 3. Process Damage (Reusing apply_damage_handler logic internally for multiple targets)
        if target_damage > 0:
            event.payload["hit"] = True  # Flag for potential reaction handlers
            # Apply resistances/vulnerabilities
            if damage_type in target.immunities:
                target_damage = 0
            elif damage_type in target.vulnerabilities:
                target_damage *= 2
            elif damage_type in target.resistances:
                target_damage = target_damage // 2
            target.hp.base_value -= target_damage

            if target.hp.base_value <= 0:
                if target.concentrating_on:
                    EventBus.dispatch(GameEvent(event_type="DropConcentration", source_uuid=target.entity_uuid))
            elif target_damage > 0 and target.concentrating_on:
                dc = max(10, target_damage // 2)
                msg = (
                    f"[Engine] SYSTEM ALERT: {target.name} took damage while concentrating on '{target.concentrating_on}'. "
                    f"LLM MUST prompt a Constitution saving throw (DC {dc}). Use `drop_concentration` tool if failed."
                )
                print(msg)

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

                target.active_conditions.append(
                    ActiveCondition(
                        name=cond_name,
                        duration_seconds=duration_secs,
                        source_name=event.payload.get("ability_name", "Unknown"),
                        applied_initiative=current_init,
                        source_uuid=caster.entity_uuid,
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

        results.append(
            f"[{target.name}] {hit_or_save_str}. Took {target_damage} {damage_type} damage. HP: {target.hp.base_value}"
        )

    # 5. Process Collateral Damage to Geography (Walls/Doors)
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


def resolve_attack_handler(event: GameEvent):
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

    # Evaluate Spatial Logic: Range & Cover
    dist, cover = spatial_service.get_distance_and_cover(attacker.entity_uuid, target.entity_uuid, event.vault_path)
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

        if distance <= get_sense_range("truesight"):
            return True
        if distance <= get_sense_range("blindsight"):
            return True
        if distance <= get_sense_range("tremorsense") and "flying" not in target_ent.tags:
            return True

        target_invisible = "invisible" in target_ent.tags or any(
            c.name.lower() in ["invisible", "hidden"] for c in target_ent.active_conditions
        )
        if target_invisible:
            return False

        if target_illumination == "darkness" and distance > get_sense_range("darkvision"):
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

    # Evaluate Advanced Condition Framework for Attacks
    attacker_conds = [c.name.lower() for c in attacker.active_conditions]
    target_conds = [c.name.lower() for c in target.active_conditions]

    if any(c in attacker_conds for c in ["restrained", "poisoned", "prone", "frightened"]):
        print(f"[Engine] {attacker.name} is hampered by a condition. Applying DISADVANTAGE to attack.")
        event.payload["disadvantage"] = True

    if any(c in target_conds for c in ["restrained", "stunned", "paralyzed", "petrified", "unconscious", "blinded"]):
        print(f"[Engine] {target.name} has a debilitating condition. Applying ADVANTAGE to attackers.")
        event.payload["advantage"] = True

    if "prone" in target_conds:
        if dist <= 5.0:
            event.payload["advantage"] = True
        else:
            event.payload["disadvantage"] = True

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

        total_attack = d20_roll + attack_bonus
        is_critical_hit = d20_roll == 20
        is_hit = is_critical_hit or total_attack >= target_ac
        print(
            f"[Engine] {attacker.name} rolls a {d20_roll} ({roll1}, {roll2} if adv/disadv) "
            f"+ {attack_bonus} = {total_attack} vs AC {target_ac}{cover_msg}"
        )

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
        # Roll base damage dice
        base_damage = roll_dice(weapon.damage_dice) + damage_mod.total + weapon.magic_bonus

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


def apply_damage_handler(event: GameEvent):
    """Applies final damage to a target, considering immunities, resistances, and vulnerabilities."""
    if event.status != EventStatus.POST_EVENT or not event.payload.get("hit"):
        return

    target: Creature = get_entity(event.target_uuid)
    damage = event.payload.get("damage", 0)
    damage_type = event.payload.get("damage_type", "unknown")

    if damage > 0:
        # Check for immunities first
        if damage_type in target.immunities:
            damage = 0
            print(f"[Engine] {target.name} is IMMUNE to {damage_type}!")
        else:
            # Then check for vulnerabilities and resistances
            if damage_type in target.vulnerabilities:
                damage *= 2
                print(f"[Engine] {target.name} is VULNERABLE to {damage_type}! Damage is doubled.")
            elif damage_type in target.resistances:
                damage = damage // 2  # Halve the damage, rounding down
                print(f"[Engine] {target.name} is RESISTANT to {damage_type}! Damage is halved.")

        target.hp.base_value -= damage
        print(f"[Engine] {target.name} takes {damage} {damage_type} damage. HP remaining: {target.hp.base_value}")

        if target.hp.base_value <= 0:
            if target.concentrating_on:
                EventBus.dispatch(GameEvent(event_type="DropConcentration", source_uuid=target.entity_uuid))
        elif damage > 0 and target.concentrating_on:
            dc = max(10, damage // 2)
            msg = (
                f"[Engine] SYSTEM ALERT: {target.name} took damage while concentrating on '{target.concentrating_on}'. "
                f"LLM MUST prompt a Constitution saving throw (DC {dc}). Use `drop_concentration` tool if failed."
            )
            print(msg)


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
                    maximum = match.group(2)
                    target.resources[res_name] = f"{maximum}/{maximum}"
            print(f"[Engine] {target.name} finished a Long Rest. HP and resources fully restored.")


def handle_advance_time_event(event: GameEvent):
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

    caster.concentrating_on = ""


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

    # 2. Check Prone mechanics
    stand_cost = 0
    if "prone" in active_conds and movement_type not in ["crawl"]:
        stand_cost = entity.speed // 2
        if entity.movement_remaining < stand_cost:
            event.status = EventStatus.CANCELLED
            event.payload["error"] = (
                f"Movement failed. {entity.name} is Prone and does not have enough movement "
                f"({stand_cost}ft needed) to stand up. Try movement_type='crawl'."
            )
            return
        entity.active_conditions = [c for c in entity.active_conditions if c.name.lower() != "prone"]
        print(f"[Engine] {entity.name} spends {stand_cost}ft of movement to stand up from Prone.")

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

    # Truncate to 2 decimal places to eliminate floating point noise before applying math.ceil
    raw_dist = normal_dist + (diff_dist * 2)
    total_cost = math.ceil(int(raw_dist * 100) / 100.0) + stand_cost

    if event.payload.get("dragged_uuids"):
        total_cost *= 2  # Dragging halves speed (costs twice as much movement per foot)

    if total_cost > entity.movement_remaining and not event.payload.get("ignore_budget", False):
        event.status = EventStatus.CANCELLED
        event.payload["error"] = (
            f"Movement cost ({total_cost}ft) exceeds remaining speed ({entity.movement_remaining}ft). "
            f"Normal dist: {normal_dist:.1f}ft, Difficult dist: {diff_dist:.1f}ft."
        )
        return

    event.payload["cost"] = total_cost


def resolve_movement_handler(event: GameEvent):
    """Evaluates movement to see if it provokes opportunity attacks."""
    if event.status != EventStatus.EXECUTION:
        return

    movement_type = event.payload.get("movement_type", "walk").lower()

    entity: Creature = get_entity(event.source_uuid)
    target_x = event.payload.get("target_x")
    target_y = event.payload.get("target_y")
    target_z = event.payload.get("target_z", entity.z)

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
        trap.is_active = False  # Deactivate after triggering once
        print(f"[Engine] TRAP TRIGGERED: {trap.hazard_name} during movement!")

        target_uuids = {entity.entity_uuid}
        if trap.radius > 0:
            spatial_hits = spatial_service.get_targets_in_radius(ox, oy, trap.radius, event.vault_path)
            target_uuids.update(spatial_hits)

        trap_source = Creature(
            name=trap.hazard_name,
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


def register_core_handlers():
    """Registers all standard handlers to the Event Bus. Can be called to reset state."""
    EventBus._listeners.clear()
    EventBus.subscribe("MeleeAttack", shield_spell_reaction_handler, priority=1)
    EventBus.subscribe("MeleeAttack", resolve_attack_handler, priority=10)
    EventBus.subscribe("MeleeAttack", apply_damage_handler, priority=100)
    EventBus.subscribe("SpellCast", shield_spell_reaction_handler, priority=1)
    EventBus.subscribe("SpellCast", resolve_spell_cast_handler, priority=10)
    EventBus.subscribe("SpellCast", trap_noise_handler, priority=100)
    EventBus.subscribe("SavingThrow", evasion_save_handler, priority=10)
    EventBus.subscribe("Rest", handle_rest_event, priority=10)
    EventBus.subscribe("AdvanceTime", handle_advance_time_event, priority=10)
    EventBus.subscribe("DropConcentration", handle_drop_concentration_event, priority=10)
    EventBus.subscribe("Movement", validate_movement_handler, priority=5)
    EventBus.subscribe("Movement", resolve_movement_handler, priority=10)
    EventBus.subscribe("Movement", trap_movement_handler, priority=15)
    EventBus.subscribe("Movement", consume_movement_handler, priority=100)


# Register handlers automatically when imported
register_core_handlers()

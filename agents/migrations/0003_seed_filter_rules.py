"""Seed the default stage-2 detection rules so they are editable in the UI."""
from django.db import migrations


def seed_rules(apps, schema_editor):
    FilterRule = apps.get_model("agents", "FilterRule")
    from agents.engine.filters import DEFAULT_RULE_SEEDS

    for rule_type, pattern in DEFAULT_RULE_SEEDS:
        FilterRule.objects.get_or_create(rule_type=rule_type, pattern=pattern)


def unseed_rules(apps, schema_editor):
    FilterRule = apps.get_model("agents", "FilterRule")
    from agents.engine.filters import DEFAULT_RULE_SEEDS

    for rule_type, pattern in DEFAULT_RULE_SEEDS:
        FilterRule.objects.filter(rule_type=rule_type, pattern=pattern).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0002_filterrule_propagationjob_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_rules, unseed_rules),
    ]

"""Duration must not trap recommendations in the player's recent short maps."""
import unittest

from osu_coach.core import engine
from osu_coach.settings import settings_context
from tests.test_engine import NOW, play, beatmap


class DurationRecommendationTests(unittest.TestCase):
    def profile(self):
        return engine.assess([play(i, length=30) for i in range(8)], NOW)

    def selected(self, maps, profile, stage):
        return engine.recommend(maps, profile, stages={stage}, limit=3, fill_online=True)[0]['maps']

    def test_long_maps_remain_eligible_in_every_stage_and_source(self):
        profile = self.profile()
        for stage in ('warmup', 'practice', 'challenge'):
            target = engine.recommend([], profile, stages={stage})[0]['target']
            for source in ('local', 'online'):
                with self.subTest(stage=stage, source=source):
                    maps = [beatmap(i, stars=target, source=source, length=length)
                            for i, length in enumerate((180, 360, 900))]
                    self.assertEqual(3, len(self.selected(maps, profile, stage)))

    def test_length_does_not_change_practice_or_consolidation_ranking_even_without_history(self):
        for profile in (self.profile(), engine.assess([], NOW)):
            for stage in ('practice', 'challenge'):
                with self.subTest(phase=profile['phase'], stage=stage):
                    target = engine.recommend([], profile, stages={stage})[0]['target']
                    maps = [beatmap(i, stars=target, length=30) for i in range(5)]
                    before = self.selected(maps, profile, stage)
                    varied = [dict(m, length=900 if i == 0 else 30 + i * 100) for i, m in enumerate(maps)]
                    after = self.selected(varied, profile, stage)
                    self.assertEqual([m['key'] for m in before], [m['key'] for m in after])

    def test_warmup_preference_is_configurable_soft_and_never_blocks_long_maps(self):
        profile = self.profile()
        target = engine.recommend([], profile, stages={'warmup'})[0]['target']
        long = beatmap(0, stars=target, length=300)
        short = beatmap(1, stars=target, length=60)
        maps = [long, short]
        self.assertEqual(short['key'], self.selected(maps, profile, 'warmup')[0]['key'])
        for seconds in (0, 300):
            with self.subTest(seconds=seconds), settings_context({'warmup_preferred_seconds': seconds}):
                self.assertEqual(long['key'], self.selected(maps, profile, 'warmup')[0]['key'])
        # Better difficulty fit outweighs even the largest duration preference.
        short['stars'] = target + .1
        long['length'] = 3600
        self.assertEqual(long['key'], self.selected(maps, profile, 'warmup')[0]['key'])

    def test_duration_does_not_change_measured_level(self):
        short = [play(i, length=30) for i in range(8)]
        longer = [dict(p, length=600) for p in short]
        self.assertEqual(engine.assess(short, NOW)['baseline'], engine.assess(longer, NOW)['baseline'])

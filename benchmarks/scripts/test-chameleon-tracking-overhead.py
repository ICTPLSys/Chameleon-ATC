import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('overhead',Path(__file__).with_name('chameleon-tracking-overhead.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def pair(delta, latency=None):
    off={'status':'PASS','memory_mib':32768,'workload_configuration':{'threads':8},'performance':{'cost':100}}
    on={**off,'performance':{'cost':100+delta}}
    if latency is not None:
        off['performance']['p95_us']=100
        on['performance']['p95_us']=100+latency
    return {'off':off,'on':on}

class Acceptance(unittest.TestCase):
    def test_single_outlier_fails_despite_mean(self):
        self.assertEqual(m.evaluate([pair(-2),pair(-2),pair(6)])['status'],'FAIL')
    def test_mean_fails_despite_individual_passes(self):
        self.assertEqual(m.evaluate([pair(3)]*3)['status'],'FAIL')
    def test_negative_measurements_are_retained(self):
        result=m.evaluate([pair(-1),pair(0),pair(1)])
        self.assertEqual(result['status'],'PASS')
        self.assertLess(result['pairs'][0]['slowdown_percent'],0)
    def test_kv_latency_must_also_pass(self):
        self.assertEqual(m.evaluate([pair(0,10)]*3)['status'],'FAIL')
    def test_failed_mechanism_cannot_pass(self):
        p=pair(0);p['on']['status']='FAIL'
        self.assertEqual(m.evaluate([p]*3)['status'],'INVALID')
    def test_incomplete_cannot_pass(self):
        self.assertEqual(m.evaluate([pair(0)])['status'],'INCOMPLETE')
    def test_different_spark_seeds_are_not_a_pair(self):
        p=pair(0)
        p['off']['workload_identity']={'initialization_seed':'42'}
        p['on']['workload_identity']={'initialization_seed':'43'}
        self.assertEqual(m.evaluate([p]*3)['status'],'INVALID')

if __name__=='__main__':unittest.main()

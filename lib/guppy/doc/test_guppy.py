# Tests generated by: guppy.gsl.Tester
# Main source file: /home/sverker/guppy/specs/genguppy.gsl
# Date: Tue Jun 23 16:15:55 2009
class Tester:
    tests = {}
    def test_guppy(self, arg):
        t0 = arg.hpy()
        t1 = arg.hpy(ht=())
        t2 = arg.Root()
    tests['.tgt.kindnames.guppy'] = test_guppy
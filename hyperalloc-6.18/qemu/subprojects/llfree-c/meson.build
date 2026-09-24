project('llfree-c', 'c')

cflags = ['-std=c11', '-Werror', '-Wunused-variable', '-Wundef', '-Werror=strict-prototypes', '-Wno-trigraphs', '-Werror=implicit-function-declaration', '-Werror=implicit-int', '-Werror=return-type', '-Wno-format-security', '-Werror=unknown-warning-option', '-Werror=ignored-optimization-argument', '-Wno-sign-compare', '-Wno-frame-address', '-Wno-address-of-packed-member', '-Wno-gnu', '-Wno-unused-const-variable', '-Wvla', '-Wno-pointer-sign', '-Wimplicit-fallthrough', '-Werror=date-time', '-Werror=incompatible-pointer-types', '-Wno-initializer-overrides', '-Wno-sign-compare', '-Wno-pointer-to-enum-cast', '-Wno-tautological-constant-out-of-range-compare', '-fPIE', '-pthread']

private_inc = include_directories('llc/src', 'llc/include', 'llc/std')
pub_inc = include_directories('llc/include', 'llc/std')

sources = ['llc/src/bitfield.c', 'llc/src/child.c', 'llc/src/llfree.c', 'llc/src/local.c', 'llc/src/lower.c', 'llc/src/tree.c']

libllc = static_library('llc',
                        sources,
                        include_directories : private_inc,
                        install : false,
                        c_args : cflags)

libllc_public_dep = declare_dependency(include_directories : pub_inc,
                        link_with : [libllc])

libllc_private_dep = declare_dependency(include_directories : private_inc,
                        link_with : [libllc])

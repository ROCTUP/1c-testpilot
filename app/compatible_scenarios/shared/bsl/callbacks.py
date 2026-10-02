"""Callbacks into a scenario module, without executing external platform code."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleContext:
    vm: object


@dataclass(frozen=True)
class CallbackDescription:
    module: ModuleContext
    name: str
    additional: object = None

    def invoke(self):
        return self.module.vm.invoke_function(self.name, [self.additional])


def construct(vm, values):
    if not 2 <= len(values) <= 3:
        vm.fail('CallbackDescription requires a function name, ThisObject and optional additional parameters.')
    name, module = values[:2]
    if not isinstance(name, str) or not isinstance(module, ModuleContext):
        vm.fail('CallbackDescription supports functions from a scenario module addressed by ThisObject.')
    name = name.casefold()
    if name not in module.vm.program.functions:
        vm.fail(f'Callback function {name!r} is not defined in this module.')
    params, _ = module.vm.program.functions[name].args
    if len(params) != 1:
        vm.fail('The condition callback must accept one additional-parameters argument.')
    return CallbackDescription(module, name, values[2] if len(values) > 2 else None)

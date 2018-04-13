import os
import shutil
from collections import Counter
from collections import defaultdict, OrderedDict
from os.path import join
from typing import List

import yaml
from CppHeaderParser import CppHeaderParser

import generators.dependency_tree
from generators.constants import common_includes, PCL_BASE, PATH_LOADER, PATH_MODULES, MODULES_TO_BUILD, \
    HEADERS_TO_SKIP, ATTRIBUTES_TO_SKIP, CLASSES_TO_IGNORE, METHODS_TO_SKIP, SUBMODULES_TO_SKIP
from generators.definitions import method_parameters
from generators.definitions.method import split_methods_by_type
from generators.definitions.submodule_loader import generate_loader
from generators.definitions.templated_class_definition import ClassDefinition
from generators.definitions.templated_class_instantiations import TemplatedClassInstantiations
from generators.point_types_utils import unpack_yaml_point_types
from generators.utils import make_header_include_name, sort_headers_by_dependencies, \
    generate_main_loader, explicit_includes, make_namespace_class


def filter_methods_for_parser_errors(methods):
    return [m for m in methods if not m["name"] in ("void", "bool")]


def filter_methods_to_skip(methods):
    filtered_methods = []
    for m in methods:
        if (m["parent"]["name"], m["name"]) in METHODS_TO_SKIP:
            continue
        if "Callback" in m["name"]:
            continue
        # "Double pointer arguments are not supported by pybind11."  -wjakob
        parameters_types = [p["type"] for p in m["parameters"]]
        if any("**" in type_.replace(" ", "") for type_ in parameters_types):
            continue
        filtered_methods.append(m)
    return filtered_methods


def gen_class_function_definitions(main_classes, module, header_name, path, needs_overloading: List[str]) -> List[str]:
    text = [common_includes]
    text.append(explicit_includes(module, header_name))
    text.append(make_header_include_name(module, header_name, path))
    text.append("")

    namespaces = set([c["namespace"] for c in main_classes])
    for namespace in namespaces:
        if not namespace == "pcl":
            text.append("using namespace %s;" % namespace)
    text.append("\n")

    for class_ in main_classes:
        doc = class_.get("doxygen", "")
        methods = class_["methods"]["public"]
        methods = filter_methods_for_parser_errors(methods)
        methods = filter_methods_to_skip(methods)
        class_properties = [p for p in class_["properties"]["public"]
                            if not "using" in p["type"]
                            and not "union" in p["type"]]
        union_properties = [p for nested_class in class_["nested_classes"]
                            for p in nested_class["properties"]["public"]
                            if "union" in nested_class["name"]]
        class_properties += union_properties
        class_properties = filter_class_properties(module, header_name, class_["name"], class_properties)
        constructors, variables, others = split_methods_by_type(methods, class_properties,
                                                                needs_overloading)
        if not class_["can_be_instantiated"]:
            constructors = []
        class_def = ClassDefinition(class_, constructors, variables, others, module)
        text.append(class_def.to_class_function_definition())
        text.append("")

    return text


def filter_class_properties(module, header, class_name, properties):
    key = (module, header, class_name)
    # ignore properties without a name
    properties = [p for p in properties if p["name"]]
    if key in ATTRIBUTES_TO_SKIP:
        to_ignore = ATTRIBUTES_TO_SKIP[key]
        filtered_properties = []
        for p in properties:
            if p["name"] in to_ignore:
                continue
            filtered_properties.append(p)
        properties = filtered_properties
    return properties


def get_main_classes(header, module, header_name):
    # header = read_headers(base_path, header_name, module)
    main_classes = [c for c in header.classes.values() if c["namespace"] in ("pcl", "pcl::" + module)]
    filtered_main_classes = []
    for class_ in main_classes:
        if class_.get("template") and class_["template"].startswith("template <>"):
            message = "Warning: Template class specialization not implemented for class %s in %s"
            print(message % (class_["name"], header_name))
        elif (module, header_name, class_["name"]) in CLASSES_TO_IGNORE:
            pass
        else:
            filtered_main_classes.append(class_)
    filtered_main_classes = sorted(filtered_main_classes, key=lambda c: c["name"])
    return filtered_main_classes


def replace_some_terms(raw_lines):
    lines = []
    append = lines.append
    for line in raw_lines:
        line_strip = line.strip()
        if line_strip.startswith("BOOST_CONCEPT_"):
            pass
        elif line_strip.startswith("BOOST_MPL_ASSERT"):
            pass
        elif line_strip.startswith("PCL_DEPRECATED"):
            pass
        elif line_strip.startswith("POINT_CLOUD_REGISTER_POINT_STRUCT"):
            pass
        else:
            append(line)
    text = "".join(lines)
    text = text.replace("EIGEN_ALIGN16", "")
    text = text.replace("PCL_EXPORTS", "")
    text = text.replace("<void ()>", "")  # parser chokes on "boost::function<void ()>"
    text = text.replace("->operator", "-> operator")  # parser error for this expression
    return text


def read_header(header_path):
    # I tried to do this in multiple threads but it seems like CppHeaderParser is not thread safe...
    try:
        header_file_str = replace_some_terms(open(header_path, encoding="utf8").readlines())
    except UnicodeDecodeError:
        header_file_str = replace_some_terms(open(header_path).readlines())
    parser = CppHeaderParser
    parser.debug = False
    header = parser.CppHeader(header_file_str, argType="string")
    return header


def clean():
    try:
        os.remove(PATH_LOADER)
    except FileNotFoundError:
        pass
    if os.path.exists(PATH_MODULES):
        shutil.rmtree(PATH_MODULES)


def check_if_needs_overloading(main_classes):
    needs_overloading = {}
    classes_by_module = defaultdict(list)
    for (module, _), class_ in main_classes.items():
        classes_by_module[module] += class_

    for module, classes in classes_by_module.items():
        needs = []
        for class_ in classes:
            count = Counter(m["name"] for methods in class_["methods"].values() for m in methods)
            for name, count in count.items():
                if count >= 2:
                    needs.append(name)
        needs_overloading[module] = needs

    return needs_overloading


def get_headers(modules=None):
    def listmod(module):
        found_modules = []
        for base, folders, files in os.walk(join(PCL_BASE, module)):
            if any(base.endswith(m) for m in SUBMODULES_TO_SKIP):
                continue
            relative_base = os.path.abspath(base).replace(PCL_BASE, "")[1:]
            for f in files:
                if f.endswith(".h"):
                    found_modules.append([f, join(relative_base, f)])
        return found_modules

    if modules is None:
        modules = MODULES_TO_BUILD

    headers_to_generate = [(module, header_name, path) for module in modules
                           for header_name, path in listmod(module)]
    base_headers = [("", f, f) for f in os.listdir(PCL_BASE) if f.endswith(".h")]
    headers_to_generate += base_headers

    headers_to_generate_temp = []
    for module, header_name, path in headers_to_generate:
        if (module, header_name) in HEADERS_TO_SKIP:
            continue
        headers_to_generate_temp.append(tuple([module, header_name, path]))

    return headers_to_generate_temp


def get_pure_virtual_methods(class_: CppHeaderParser.CppClass):
    pure_virtual = []
    for access in ["private", "protected", "public"]:
        for m in class_["methods"][access]:
            if m["pure_virtual"]:
                pure_virtual.append(m["name"])
    return pure_virtual


def flag_instantiatable_class(dependency_tree, main_classes):
    # determine if the class can be instantiated
    main_classes_by_name_namespace = {make_namespace_class(c["namespace"], c["name"]): c
                                      for classes in main_classes.values() for c in classes}

    for module, header_name in main_classes:
        for class_ in main_classes[(module, header_name)]:
            can_be_instantiated = True
            if class_["abstract"]:
                can_be_instantiated = False
            else:  # check for inherited abstract base classes
                methods = set([m["name"] for access in "private protected public".split()
                               for m in class_["methods"][access]])
                namespace_class = make_namespace_class(class_["namespace"], class_["name"])
                for base_name_nsp in dependency_tree.breadth_first_iterator(namespace_class):
                    base_class = main_classes_by_name_namespace.get(base_name_nsp)
                    if base_class and base_class["abstract"]:
                        base_pure_virtual_methods = set(get_pure_virtual_methods(base_class))
                        if base_pure_virtual_methods - methods:
                            can_be_instantiated = False
            class_["can_be_instantiated"] = can_be_instantiated


def generate(headers_to_generate) -> OrderedDict:
    """
    :return: OrderedDict
    """
    classes_point_types = unpack_yaml_point_types("point_types_generated.yml")
    extra_point_types = unpack_yaml_point_types("point_types_extra.yml")
    for k, v in extra_point_types.items():
        if k in classes_point_types:
            classes_point_types[k].append(v)
        else:
            classes_point_types[k] = v
    other_types = yaml.load(open("point_types_other.yml"))

    import time

    t = time.time()

    main_classes = {}

    for module, header_name, path in headers_to_generate[:]:
        try:
            header_full_path = join(PCL_BASE, path) if path else join(PCL_BASE, module, header_name)
            header = read_header(header_full_path)
            main_classes[(module, header_name)] = get_main_classes(header, module, header_name)
        except CppHeaderParser.CppParseError:
            print("Warning: skipped header (%s/%s)" % (module, header_name))
            headers_to_generate.remove((module, header_name, path))

    print("read header in %.2f s" % (time.time() - t,))

    classes = [c for module, header, path in headers_to_generate
               for c in main_classes[(module, header)]]

    dependency_tree = generators.dependency_tree.DependencyTree(classes)

    point_types = dependency_tree.get_point_types_with_dependencies(classes_point_types)

    sorted_base_classes_first = list(dependency_tree.leaf_iterator())

    key = lambda x: sorted_base_classes_first.index(make_namespace_class(x["namespace"], x["name"]))
    for module, header in main_classes:
        main_classes[(module, header)] = list(sorted(main_classes[(module, header)], key=key))

    headers_to_generate = sort_headers_by_dependencies(headers_to_generate)

    methods_needs_overloading = check_if_needs_overloading(main_classes)

    flag_instantiatable_class(dependency_tree, main_classes)

    # for module, header in headers_to_generate:
    def generate_header(module, header, path, main_classes) -> str:
        text = gen_class_function_definitions(main_classes, module, header, path,
                                              methods_needs_overloading.get(module))
        module_def = TemplatedClassInstantiations(main_classes, module, header, point_types, other_types)
        text.append(module_def.to_module_function_definition())
        return "\n".join(text)

    generated_headers = OrderedDict()
    for module, header, path in headers_to_generate:
        generated_headers[(module, header)] = generate_header(module, header, path, main_classes[(module, header)])

    print("generated in %.2f s" % (time.time() - t,))

    return generated_headers


def make_module_dirs(modules):
    for module in modules:
        module_dir = join(PATH_MODULES, module)
        if not os.path.exists(module_dir):
            os.makedirs(module_dir)


def is_file_different(path, text):
    v = open(path).read()
    if v != text:
        print("File is different: %s" % os.path.split(path)[1])
        return True
    # print("File is the same: %s" % os.path.split(path)[1])
    return False


def write_if_different(files_to_write, delete_others):
    written = []

    for base, folder, files in os.walk(PATH_MODULES):
        for f in files:
            path = join(base, f)
            if path in files_to_write:
                if is_file_different(path, files_to_write[path]):
                    open(path, "w").write(files_to_write[path])
                    written.append(path)
            elif delete_others:
                os.remove(path)
                print("Deleted: " + path)

    # write new files
    for path, text in files_to_write.items():
        if path not in written:
            open(path, "w").write(files_to_write[path])


def write_stuff_if_needed(generated_headers: OrderedDict, delete_others=True):
    modules = set(module for module, _ in generated_headers.keys())

    make_module_dirs(modules)

    # hpp
    files_to_write = {}
    for (module, header_name), text in generated_headers.items():
        output_path = join(PATH_MODULES, module, header_name + "pp")
        files_to_write[output_path] = text

    # loaders
    default_types_by_namespace = method_parameters.all_default_types_by_namespace
    loader_modules = defaultdict(list)
    for module, header in generated_headers:
        loader_modules[module or "base"].append(header)
    for module, headers in loader_modules.items():
        path_loader = join(PATH_MODULES, "_%s_loader.cpp" % module)
        files_to_write[path_loader] = generate_loader(module, headers)

    files_to_write[PATH_LOADER] = generate_main_loader(loader_modules)

    write_if_different(files_to_write, delete_others)


def main():
    all_headers = get_headers()
    generated_headers = generate(all_headers)
    write_stuff_if_needed(generated_headers, delete_others=True)


if __name__ == '__main__':
    main()
from abc import ABCMeta, abstractmethod
import logging
from rainbow_logging_handler import RainbowLoggingHandler
import sys

from .mapper import AbstractMapper
from .checker import AbstractChecker
from graphs.generate_service import ServiceGMLGraph, InfrastructureGMLGraph


class UnfeasibleBinPacking(Exception):

    def __init__(self, msg, *args, **kwargs):
        super(UnfeasibleBinPacking, self).__init__(*args, **kwargs)
        self.msg = msg


class VolatileResourcesChecker(AbstractChecker):

    def __init__(self):
        super(VolatileResourcesChecker, self).__init__()

    def check_infra(self, infra) -> bool:
        """


        :param infra:
        :type infra: simulator.generate_service.InfrastructureGMLGraph
        :return:
        """
        return infra.check_graph()

    def check_ns(self, ns) -> bool:
        """


        :param ns:
        :type ns: simulator.generate_service.ServiceGMLGraph
        :return:
        """
        return ns.check_graph()


class Item(dict):

    def __init__(self, id, weight, node_dict, possible_bins, seq=None, mapped_to = None, **kwargs):
        """
        Class to store information about an item of the bin packing problem.
        The mapped_to key represents the Bin object bin where this is mapped, None by default.

        :param weight:  weight to be used for placement
        :param node_dict: dictionary of the correspoinding VNF read from the input
        :param seq:
        :param possible_bins: list of Bin objects where this item might possibly go.
        :param kwargs:
        """
        super(Item, self).__init__(seq=seq, id=id, node_dict=node_dict, weight=weight, **kwargs)
        self.mapped_to = mapped_to
        self.possible_bins = possible_bins

    def __repr__(self):
        return "Item(id={}, weight={}, mapped_to={})".format(self['id'], self['weight'], self.mapped_to)


class Bin(dict):

    def __init__(self, id, capacity, fixed_cost, unit_cost, node_dict, mapped_here, seq=None, **kwargs):
        """
        Class to store and calculate info for a bin of the bin packing problem.
        The mapped_here attribute stores the items mapped here.

        :param id:
        :param capacity:
        :param fixed_cost:
        :param unit_cost:
        :param node_dict:
        :param seq:
        :param kwargs:
        """
        super(Bin, self).__init__(seq=seq, id=id, capacity=capacity, fixed_cost=fixed_cost,
                                  unit_cost=unit_cost, node_dict=node_dict, **kwargs)
        self.mapped_here = mapped_here
        self.preference = None

    @property
    def filled_unit_cost(self):
        if self['capacity'] > 0:
            fixed_part = self['fixed_cost'] / self['capacity']
        else:
            fixed_part = float('inf')
        return fixed_part + self['unit_cost']

    @property
    def total_load(self):
        return sum(map(lambda i: i['weight'], self.mapped_here))

    @property
    def is_overloaded(self):
        return self['capacity'] < self.total_load

    def does_item_fit(self, item):
        return self['capacity'] >= self.total_load + item['weight']

    def get_variable_cost_of_mapping(self, item):
        return item['weight'] * self['unit_cost']

    def __repr__(self):
        return "Bin(id={}, capacity={})".format(self['id'], self['capacity'])


class BasePruningStep(metaclass=ABCMeta):

    def __init__(self):
        super(BasePruningStep, self).__init__()

    @abstractmethod
    def prune_possible_mappings(self, infra, ns, items : list, bins : list) -> tuple:
        """
        The result of the pruning must be relfected in the Item.possible_bins attribute of the results

        :param infra:
        :param ns:
        :param items: list of Items
        :param bins: list of Bins
        :return: tuple of the pruned items and bins
        """
        pass


class PruneLocalityConstraints(BasePruningStep):

    def prune_possible_mappings(self, infra : InfrastructureGMLGraph, ns : ServiceGMLGraph, items : list, bins : list):
        """
        Remove possible bins which contradict the locality constraints stored in the VNF/their corresponding Item.

        :param infra:
        :param ns:
        :param items:
        :param bins:
        :return:
        """
        for item in items:
            if ns.location_constr_str in item['node_dict']:
                # we need to make a list from the bins, otherwise we couldnt remove from it
                for bin in list(item.possible_bins):
                    if bin['id'] not in item['node_dict'][ns.location_constr_str]:
                        item.possible_bins.remove(bin)
        return items, bins


# TODO: add other pruning classes
# class ASDSDASDAS(BasePruningStep)


class ConstructiveMapperFromFractional(AbstractMapper):

    def __init__(self, checker: AbstractChecker):
        """
        Constructs a solution for the volatile resources problem based on the fractional optimal solution
        for the inherent bin packing problem as defined by Cambazard, et. al. -- Bin Packing with Linear Usage
        Costs - An Application to Energy Management in Data Centres, https://hal.archives-ouvertes.fr/hal-00858159

        :param checker:
        """
        super(ConstructiveMapperFromFractional, self).__init__(checker)
        # NOTE: names starting with __ are sort of private methods in python
        self.__checker = checker
        self.bins = []
        self.items = []
        self.pruning_steps_collection = [PruneLocalityConstraints()]
        self.objective_value_of_fractional_opt = None
        self.objective_value_of_integer_solution = None
        self.log = logging.Logger(self.__class__.__name__)
        handler = RainbowLoggingHandler(sys.stderr, color_funcName=('black', 'yellow', True))
        formatter = logging.Formatter('%(asctime)s(%(name).6s)%(levelname).3s: %(message)s')
        handler.setFormatter(formatter)
        self.log.addHandler(handler)
        self.log.setLevel(logging.INFO)

        # these might not be needed if we override the functions with other heuristics.
        self.epsilon = 1e-3
        self.min_bin_preference = None

    @property
    def total_item_weight(self):
         return sum(map(lambda i: i['weight'], self.items))

    def get_bins_sorted_by_filled_unit_cost(self):
        return sorted(self.bins, key=lambda b: b.filled_unit_cost)

    def get_base_bin_packing_problem(self, infra, ns):
        """
        Constructs a base binpacking problem without filtering out any of the possible mappings.

        :param infra:
        :param ns:
        :return:
        """
        for n, node_dict in ns.nodes(data=True):
            # TODO: fill in from values of the node based on checker.
            # TODO (we might filter out APs and endpoints here already -- If we know what exactly will be their 'type' fields)
            # initialize the problem with all possible bins
            self.items.append(Item(n, node_dict[ns.nf_demand_str], node_dict, possible_bins=[]))
        min_weighted_item = min(self.items, key=lambda i: i['weight'])
        for n, node_dict in infra.nodes(data=True):
            # TODO: fill in from values of the node based on checker.
            bin = Bin(n, node_dict[infra.infra_node_capacity_str], node_dict[infra.infra_fixed_cost_str],
                      node_dict[infra.infra_unit_cost_str], node_dict, mapped_here=[])
            if bin['capacity'] >= min_weighted_item['weight']:
                self.bins.append(bin)
            elif bin['capacity'] > self.epsilon:
                self.log.info("Discarding bin {} because it cannot fit even the smallest item".format(bin))
        if len(self.bins) == 0:
            raise UnfeasibleBinPacking("None of the bins can host the smallest item!")
        for item in self.items:
            # important to have a separate list for the possible bins for each item
            # (removing from one, Must not be reflected in another item's possible bins)
            item.possible_bins.extend(self.bins)

    def set_initial_bin_preferences(self, original_best_bins, total_bin_capacity):
        # sets the preference to the same ordering which is given by the fractional mapping variables for the best bins
        min_pref = float('inf')
        self.objective_value_of_fractional_opt = 0.0
        for bin in original_best_bins:
            if bin is original_best_bins[-1]:
                # the last item has less preference than its capacity
                bin.preference = self.total_item_weight - (total_bin_capacity - bin['capacity'])
            else:
                bin.preference = bin['capacity']
            self.objective_value_of_fractional_opt += bin['fixed_cost'] + bin.preference * bin['unit_cost']
            if bin.preference < min_pref:
                min_pref = bin.preference
        self.min_bin_preference = min_pref
        self.log.debug("Minimum bin preference set to {}".format(self.min_bin_preference))

    def get_fist_best_bins(self):
        """
        Gets the first 'k' best bins according to the paper's definition in section 2.1.
        This fully defines the fractional optimal solution.

        :return: sorted best bins
        """
        sorted_bins = self.get_bins_sorted_by_filled_unit_cost()
        total_bin_capacity = 0.0
        best_bins = []
        for bin in sorted_bins:
            total_bin_capacity += bin['capacity']
            best_bins.append(bin)
            if total_bin_capacity >= self.total_item_weight:
                self.set_initial_bin_preferences(best_bins, total_bin_capacity)
                return best_bins
        else:
            raise UnfeasibleBinPacking("Total item weight {} is more than all the bin capacities {}".
                                       format(self.total_item_weight, total_bin_capacity))

    def map_all_items_to_bins(self, best_bins, infra, ns):
        """
        Round the fractional optimal solution defined by the best_bins.
        Round the x_ij mapping variable to the highest one, aka, get the highest capacity bin from the
        intersection of the possible bins of an item and the input best bins
        (this appoach neglects that in the 'k'-th bin has less then it is capacity allocated, so the x_ik is lower too).
        Ignores all other constrains from infra and ns

        :param best_bins:
        :param infra:
        :param ns:
        :return:
        """
        for item in self.items:
            best_and_possible_bins = [b for b in best_bins if b in item.possible_bins]
            if len(best_and_possible_bins) > 0:
                chosen_bin = max(best_and_possible_bins, key=lambda b: b.preference)
                item.mapped_to = chosen_bin
                chosen_bin.mapped_here.append(item)
            elif len(item.possible_bins) == 1:
                item.mapped_to = item.possible_bins[0]
                item.possible_bins[0].mapped_here.append(item)
            elif len(item.possible_bins) > 1:
                raise NotImplementedError("Bin packing heuristic is not implemented for unambiguous initial mapping outside of the "
                                          "best bins provided by the fractional optimal solution")
            else:
                raise UnfeasibleBinPacking("Item {} cannot be mapped anywhere".format(item))

    def improve_item_to_bin_mappings(self, best_bins, infra, ns):
        """
        Moves the item, which increases the objective the least, to one of the best bins where it fits.

        :param best_bins:
        :param infra:
        :param ns:
        :return: bool, whether there is anything left to improve
        """
        overloading_items = []
        for bin in best_bins:
            if bin.is_overloaded:
                overloading_items.extend(bin.mapped_here)
        if len(overloading_items) == 0:
            return False
        else:
            cost_of_cheapest_improvement = float('inf')
            target_bin = None
            item_to_be_moved = None
            for item in overloading_items:
                for bin in best_bins:
                    if bin.does_item_fit(item) and bin is not item.mapped_to and bin in item.possible_bins:
                        # Difference between the current mapping and the possible relocation.
                        # This value might be even negative, if the rounding did not consider taking the first fitting bin in the
                        # ordered best bin list.
                        cost_of_improvement = bin.get_variable_cost_of_mapping(item) - \
                                              item.mapped_to.get_variable_cost_of_mapping(item)
                        if cost_of_improvement < cost_of_cheapest_improvement:
                            cost_of_cheapest_improvement = cost_of_improvement
                            target_bin = bin
                            item_to_be_moved = item
            if target_bin is not None:
                self.log.debug("Improving mapping by moving item {} to target bin {}".
                               format(item_to_be_moved, target_bin))
                # delete the mapping of the foudn item from its current mapping
                if item_to_be_moved not in item_to_be_moved.mapped_to.mapped_here:
                    raise Exception("Item is not foudn in mapped_to of a bin where it should have been!")
                item_to_be_moved.mapped_to.mapped_here.remove(item_to_be_moved)
                target_bin.mapped_here.append(item_to_be_moved)
                # set its mapping to the target bin
                item_to_be_moved.mapped_to = target_bin
                # NOTE: even if this is the very last improvement, it will turn out in the next call of this function
                return True
            else:
                return False

    def get_new_best_bins(self, best_bins, infra, ns) -> tuple:
        """
        Returns the new list of the best bins (moving/adding possible),
        and bool to indicate wether we can add another bin if necessary.
        This implementation always only adds the next bin according to their filled unit cost.

        :param best_bins:
        :param infra:
        :param ns:
        :return:
        """
        if self.check_bin_mapping():
            # we dont have to add next bin, everything is mapped to the current best bins
            return best_bins, False
        else:
            for bin in self.get_bins_sorted_by_filled_unit_cost():
                if bin not in best_bins:
                    best_bins.append(bin)
                    # all later introduced bins are less and less preferred
                    bin.preference = self.min_bin_preference - self.epsilon
                    self.min_bin_preference = bin.preference
                    self.log.info("Introducing next new bin {} with minimal preference {}".format(bin, bin.preference))
                    # we return with the first one right away
                    return best_bins, True
            else:
                # it means, that all bins are already in the best bins.
                return best_bins, False

    def check_bin_mapping(self):
        """
        Checks if the constructed solution for the bin packing is valid.
        Also calculates the objective_value_of_integer_solution if the solution is valid.

        :return:
        """
        self.objective_value_of_integer_solution = 0.0
        for item in self.items:
            if item.mapped_to is None:
                self.objective_value_of_integer_solution = None
                return False
            else:
                self.objective_value_of_integer_solution += item.mapped_to.get_variable_cost_of_mapping(item)
        all_items = list(self.items)
        for bin in self.bins:
            if bin.is_overloaded:
                self.objective_value_of_integer_solution = None
                return False
            elif len(bin.mapped_here) > 0:
                for item in bin.mapped_here:
                    if item in all_items:
                        all_items.remove(item)
                    else:
                        raise Exception("Wrong item mapping structure, each item must be in exactly one bin!")
                self.objective_value_of_integer_solution += bin['fixed_cost']
        if len(all_items) != 0:
            raise Exception("Item not found in mapped_here structure in any bin!")
        return True

    def check_other_constraints(self, infra : InfrastructureGMLGraph, ns : ServiceGMLGraph):
        """
        Checks whether the output indeed satisfies all volatile resources constraints (capacity, can be checked by
        the check_bin_mapping function)

        :return:
        """
        for item in self.items:
            if ns.location_constr_str in item['node_dict']:
                if item.mapped_to['id'] not in item['node_dict'][ns.location_constr_str]:
                    return False
        return True

    def construct_output_mapping(self, mapping):
        """


        :param mapping:
        :return:
        """
        mapping['worked'] = True

        return mapping

    # TODO: maybe return a mapping structure as a class? (we could move retrieving mapping info to this class instead of static mapper functions)
    def map(self, infra, ns) -> dict:
        mapping = {
            'worked': False
        }
        # Check that graphs have correct format
        if not self.__checker.check_infra(infra) or not self.__checker.check_ns(ns):
            return mapping

        self.get_base_bin_packing_problem(infra, ns)
        for pruning in self.pruning_steps_collection:
            self.items, self.bins = pruning.prune_possible_mappings(infra, ns, self.items, self.bins)

        # get fractional solution: it is completely defined by listing the first 'k' bins according to
        # the definition of the paper in section 2.1.
        # NOTE: 'k' = len(best_bins)
        try:
            best_bins = self.get_fist_best_bins()
            # get rounding : map all items somewhere, not neccessarily respecting the constraints.
            # NOTE: With another heuristic it might be needed to be run again, after a new bin is introduced.
            self.map_all_items_to_bins(best_bins, infra, ns)
            can_add_next_bin = True
            while can_add_next_bin:
                anything_left_to_improve = True
                while anything_left_to_improve:
                    # get mapping improvement : improve on the item mappings
                    anything_left_to_improve = self.improve_item_to_bin_mappings(best_bins, infra, ns)
                # get new bin : if there is nothing left to improve with the current bins, we can introduce new ones
                best_bins, can_add_next_bin = self.get_new_best_bins(best_bins, infra, ns)
        except UnfeasibleBinPacking as ubp:
            self.log.exception(ubp.msg)
            raise ubp
            # TODO: for development keep it raised!
            # mapping['worked'] = False
            # return mapping

        if not self.check_bin_mapping():
            self.log.info("Bin packing solution not found by the heuristic!")
            return mapping
        elif not self.check_other_constraints(infra, ns):
            self.log.error("Bin packing result does not respect some non-bin packing constraint!")
            raise Exception("Bin packing result does not respect some non-bin packing constraint!")
        else:
            self.log.info("Bin packing solution found with objective value {}, while fractional optimal value is {}".
                          format(self.objective_value_of_integer_solution, self.objective_value_of_fractional_opt))
            mapping = self.construct_output_mapping(mapping)

        return mapping


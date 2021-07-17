import logging
from functools import partial
from itertools import repeat, zip_longest
from os import PathLike
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union
from warnings import warn

import pandas as pd

from ixmp.backend import ItemType
from ixmp.core.platform import Platform
from ixmp.core.timeseries import TimeSeries
from ixmp.model import get_model
from ixmp.utils import as_str_list, check_year, parse_url

log = logging.getLogger(__name__)


class Scenario(TimeSeries):
    """Collection of model-related data.

    See :class:`.TimeSeries` for the meaning of parameters `mp`, `model`, `scenario`,
    `version`, and `annotation`.

    Parameters
    ----------
    scheme : str, optional
        Use an explicit scheme to initialize the new scenario. The
        :meth:`~.base.Model.initialize` method of the corresponding :class:`.Model`
        class in :data:`.MODELS` is used to initialize items in the Scenario.
    """

    #: Scheme of the Scenario.
    scheme = None

    def __init__(
        self,
        mp: Platform,
        model: str,
        scenario: str,
        version: Optional[Union[int, str]] = None,
        scheme: Optional[str] = None,
        annotation: Optional[str] = None,
        **model_init_args,
    ) -> None:
        # Check arguments
        if version == "new" and scheme is None:
            log.info(f"No scheme for new Scenario {model}/{scenario}")
            scheme = ""

        if "cache" in model_init_args:
            warn(
                "Scenario(..., cache=...) is deprecated; use Platform(..., "
                "cache=...) instead",
                DeprecationWarning,
            )
            model_init_args.pop("cache")

        # Call the parent constructor
        super().__init__(
            mp=mp,
            model=model,
            scenario=scenario,
            version=version,
            scheme=scheme,
            annotation=annotation,
        )

        if self.scheme == "MESSAGE" and self.__class__ is Scenario:
            # Loaded scenario has an improper scheme
            raise RuntimeError(
                f"{model}/{scenario} is a MESSAGE-scheme scenario; use "
                "message_ix.Scenario()"
            )

        # Retrieve the Model class correlating to the *scheme*
        model_class = get_model(self.scheme).__class__

        # Use the model class to initialize the Scenario
        model_class.initialize(self, **model_init_args)

    @classmethod
    def from_url(cls, url: str, errors="warn") -> Tuple[Optional["Scenario"], Platform]:
        """Instantiate a Scenario given an ixmp-scheme URL.

        The following are equivalent::

            from ixmp import Platform, Scenario
            mp = Platform(name='example')
            scen = Scenario(mp 'model', 'scenario', version=42)

        and::

            from ixmp import Scenario
            scen, mp = Scenario.from_url('ixmp://example/model/scenario#42')

        Parameters
        ----------
        url : str
            See :meth:`parse_url <ixmp.utils.parse_url>`.
        errors : 'warn' or 'raise'
            If 'warn', a failure to load the Scenario is logged as a warning,
            and the platform is still returned. If 'raise', the exception
            is raised.

        Returns
        -------
        scenario, platform : 2-tuple of (Scenario, :class:`Platform`)
            The Scenario and Platform referred to by the URL.
        """
        assert errors in ("warn", "raise"), "errors= must be 'warn' or 'raise'"

        platform_info, scenario_info = parse_url(url)
        platform = Platform(**platform_info)

        try:
            scenario = cls(platform, **scenario_info)
        except Exception as e:
            if errors == "warn":
                log.warning(
                    f"{e.__class__.__name__}: {e.args[0]}\n"
                    f"when loading Scenario from url: {repr(url)}"
                )
                return None, platform
            else:
                raise
        else:
            return scenario, platform

    def check_out(self, timeseries_only: bool = False) -> None:
        """Check out the Scenario.

        Raises
        ------
        ValueError
            If :meth:`has_solution` is :obj:`True`.

        See Also
        --------
        TimeSeries.check_out
        utils.maybe_check_out
        """
        if not timeseries_only and self.has_solution():
            raise ValueError(
                "This Scenario has a solution, "
                "use `Scenario.remove_solution()` or "
                "`Scenario.clone(..., keep_solution=False)`"
            )
        super().check_out(timeseries_only)

    def load_scenario_data(self) -> None:
        """Load all Scenario data into memory.

        Raises
        ------
        ValueError
            If the Scenario was instantiated with ``cache=False``.
        """
        if not getattr(self.platform._backend, "cache_enabled", False):
            raise ValueError("Cache must be enabled to load scenario data")

        for ix_type in "equ", "par", "set", "var":
            log.debug(f"Cache {repr(ix_type)} data")
            get_func = getattr(self, ix_type)
            for name in getattr(self, "{}_list".format(ix_type))():
                get_func(name)

    def idx_sets(self, name: str) -> List[str]:
        """Return the list of index sets for an item (set, par, var, equ).

        Parameters
        ----------
        name : str
            name of the item
        """
        return self._backend("item_index", name, "sets")

    def idx_names(self, name: str) -> List[str]:
        """Return the list of index names for an item (set, par, var, equ).

        Parameters
        ----------
        name : str
            name of the item
        """
        return self._backend("item_index", name, "names")

    def _keys(self, name, key_or_keys):
        if isinstance(key_or_keys, (list, pd.Series)):
            return as_str_list(key_or_keys)
        elif isinstance(key_or_keys, (pd.DataFrame, dict)):
            if isinstance(key_or_keys, dict):
                key_or_keys = pd.DataFrame.from_dict(key_or_keys, orient="columns")
            idx_names = self.idx_names(name)
            return [as_str_list(row, idx_names) for _, row in key_or_keys.iterrows()]
        else:
            return [str(key_or_keys)]

    def set_list(self) -> List[str]:
        """List all defined sets."""
        return self._backend("list_items", "set")

    def has_set(self, name: str) -> bool:
        """Check whether the scenario has a set *name*."""
        return name in self.set_list()

    def init_set(
        self, name: str, idx_sets: Sequence[str] = None, idx_names: Sequence[str] = None
    ) -> None:
        """Initialize a new set.

        Parameters
        ----------
        name : str
            Name of the set.
        idx_sets : sequence of str or str, optional
            Names of other sets that index this set.
        idx_names : sequence of str or str, optional
            Names of the dimensions indexed by `idx_sets`.

        Raises
        ------
        ValueError
            If the set (or another object with the same *name*) already exists.
        RuntimeError
            If the Scenario is not checked out (see :meth:`~TimeSeries.check_out`).
        """
        idx_sets = as_str_list(idx_sets) or []
        idx_names = as_str_list(idx_names)
        return self._backend("init_item", "set", name, idx_sets, idx_names)

    def set(
        self, name: str, filters: Dict[str, Sequence[str]] = None, **kwargs
    ) -> Union[List[str], pd.DataFrame]:
        """Return the (filtered) elements of a set.

        Parameters
        ----------
        name : str
            Name of the set.
        filters : dict
            Mapping of `dimension_name` → `elements`, where `dimension_name` is one of
            the `idx_names` given when the set was initialized (see :meth:`init_set`),
            and `elements` is an iterable of labels to include in the return value.

        Returns
        -------
        :class:`pandas.DataFrame`
        """
        return self._backend("item_get_elements", "set", name, filters)

    def add_set(
        self,
        name: str,
        key: Union[str, Sequence[str], Dict, pd.DataFrame],
        comment: str = None,
    ) -> None:
        """Add elements to an existing set.

        Parameters
        ----------
        name : str
            Name of the set.
        key : str or iterable of str or dict or :class:`pandas.DataFrame`
            Element(s) to be added. If `name` exists, the elements are appended to
            existing elements.
        comment : str or iterable of str, optional
            Comment describing the element(s). If given, there must be the same number
            of comments as elements.

        Raises
        ------
        KeyError
            If the set `name` does not exist. :meth:`init_set` must be called  before
            :meth:`add_set`.
        ValueError
            For invalid forms or combinations of `key` and `comment`.
        """
        # TODO expand docstring (here or in doc/source/api.rst) with examples, per
        #      test_scenario.test_add_set.

        if isinstance(key, list) and len(key) == 0:
            return  # No elements to add

        # Get index names for set *name*, may raise KeyError
        idx_names = self.idx_names(name)

        # Check arguments and convert to two lists: keys and comments
        if len(idx_names) == 0:
            # Basic set. Keys must be strings.
            if isinstance(key, (dict, pd.DataFrame)):
                raise ValueError(
                    "dict, DataFrame keys invalid for basic set {repr(name)}"
                )

            # Ensure keys is a list of str
            keys = as_str_list(key)
        else:
            # Set defined over 1+ other sets

            # Check for ambiguous arguments
            if comment and isinstance(key, (dict, pd.DataFrame)) and "comment" in key:
                raise ValueError("ambiguous; both key['comment'] and comment " "given")

            if isinstance(key, pd.DataFrame):
                # DataFrame of key values and perhaps comments
                try:
                    # Pop a 'comment' column off the DataFrame, convert to list
                    comment = key.pop("comment").to_list()
                except KeyError:
                    pass

                # Convert key to list of list of key values
                keys = []
                for row in key.to_dict(orient="records"):
                    keys.append(as_str_list(row, idx_names=idx_names))
            elif isinstance(key, dict):
                # Dict of lists of key values

                # Pop a 'comment' list from the dict
                comment = key.pop("comment", None)

                # Convert to list of list of key values
                keys = list(map(as_str_list, zip(*[key[i] for i in idx_names])))
            elif isinstance(key[0], str):
                # List of key values; wrap
                keys = [as_str_list(key)]
            elif isinstance(key[0], list):
                # List of lists of key values; convert to list of list of str
                keys = list(map(as_str_list, key))
            elif isinstance(key, str) and len(idx_names) == 1:
                # Bare key given for a 1D set; wrap for convenience
                keys = [[key]]
            else:
                # Other, invalid value
                raise ValueError(key)

        # Process comments to a list of str, or let them all be None
        comments = as_str_list(comment) if comment else repeat(None, len(keys))

        # Combine iterators to tuples. If the lengths are mismatched, the sentinel
        # value 'False' is filled in
        to_add = list(zip_longest(keys, comments, fillvalue=False))

        # Check processed arguments
        for e, c in to_add:
            # Check for sentinel values
            if e is False:
                raise ValueError(f"Comment {repr(c)} without matching key")
            elif c is False:
                raise ValueError(f"Key {repr(e)} without matching comment")
            elif len(idx_names) and len(idx_names) != len(e):
                raise ValueError(
                    f"{len(e)}-D key {repr(e)} invalid for "
                    f"{len(idx_names)}-D set {name}{repr(idx_names)}"
                )

        # Send to backend
        elements = ((kc[0], None, None, kc[1]) for kc in to_add)
        self._backend("item_set_elements", "set", name, elements)

    def remove_set(
        self, name: str, key: Union[str, Sequence[str], Dict, pd.DataFrame] = None
    ) -> None:
        """Delete set elements or an entire set.

        Parameters
        ----------
        name : str
            Name of the set to remove (if `key` is :obj:`None`) or from which to remove
            elements.
        key : :class:`pandas.DataFrame` or list of str, optional
            Elements to be removed from set `name`.
        """
        if key is None:
            self._backend("delete_item", "set", name)
        else:
            self._backend("item_delete_elements", "set", name, self._keys(name, key))

    def par_list(self) -> List[str]:
        """List all defined parameters."""
        return self._backend("list_items", "par")

    def has_par(self, name: str) -> bool:
        """Check whether the scenario has a parameter with that name."""
        return name in self.par_list()

    def init_par(
        self, name: str, idx_sets: Sequence[str], idx_names: Sequence[str] = None
    ) -> None:
        """Initialize a new parameter.

        Parameters
        ----------
        name : str
            Name of the parameter.
        idx_sets : sequence of str or str, optional
            Names of sets that index this parameter.
        idx_names : sequence of str or str, optional
            Names of the dimensions indexed by `idx_sets`.
        """
        idx_sets = as_str_list(idx_sets) or []
        idx_names = as_str_list(idx_names)
        return self._backend("init_item", "par", name, idx_sets, idx_names)

    def par(
        self, name: str, filters: Dict[str, Sequence[str]] = None, **kwargs
    ) -> pd.DataFrame:
        """Return parameter data.

        If `filters` is provided, only a subset of data, matching the filters, is
        returned.

        Parameters
        ----------
        name : str
            Name of the parameter
        filters : dict (str -> list of str), optional
            Index names mapped to lists of index set elements. Elements not appearing
            in the respective index set(s) are silently ignored.
        """
        if len(kwargs):
            raise DeprecationWarning(
                "ignored kwargs to Scenario.par(); will raise TypeError in 4.0"
            )
        return self._backend("item_get_elements", "par", name, filters)

    def items(self, type=ItemType.PAR, filters: Dict[str, Sequence[str]] = None):
        """Iterate over model data items.

        Parameters
        ----------
        type : ItemType, optional
            Types of items to iterate, e.g. :data:`ItemType.PAR` for parameters, the
            only value currently supported.
        filters : dict, optional
            Filters for values along dimensions; same as the `filters` argument to
            :meth:`par`.

        Yields
        ------
        (str, object)
            Tuples of item name and data.
        """
        if type != ItemType.PAR:
            raise NotImplementedError(
                f"Scenario.items(type={type}); only ItemType.PAR is supported"
            )

        filters = filters or dict()

        names = sorted(self.par_list())

        for name in sorted(names):
            idx_names = set(self.idx_names(name))
            if len(filters) and not set(filters.keys()) & idx_names:
                # No overlap between the filters and this item's dimensions
                continue

            # Retrieve the data, reducing the filters to only the dimensions of
            # the item
            yield name, self.par(
                name, filters={k: v for k, v in filters.items() if k in idx_names}
            )

    def add_par(
        self,
        name: str,
        key_or_data: Union[str, Sequence[str], Dict, pd.DataFrame] = None,
        value=None,
        unit: str = None,
        comment: str = None,
    ) -> None:
        """Set the values of a parameter.

        Parameters
        ----------
        name : str
            Name of the parameter.
        key_or_data : str or iterable of str or range or dict or
                      :class:`pandas.DataFrame`
            Element(s) to be added.
        value : numeric or iterable of numeric, optional
            Values.
        unit : str or iterable of str, optional
            Unit symbols.
        comment : str or iterable of str, optional
            Comment(s) for the added values.
        """
        # Number of dimensions in the index of *name*
        idx_names = self.idx_names(name)
        N_dim = len(idx_names)

        # Convert valid forms of arguments to pd.DataFrame
        if isinstance(key_or_data, dict):
            # dict containing data
            data = pd.DataFrame.from_dict(key_or_data, orient="columns")
        elif isinstance(key_or_data, pd.DataFrame):
            data = key_or_data.copy()
            if value is not None:
                if "value" in data.columns:
                    raise ValueError("both key_or_data.value and value supplied")
                else:
                    data["value"] = value
        else:
            # One or more keys; convert to a list of strings
            if isinstance(key_or_data, range):
                key_or_data = list(key_or_data)
            keys = self._keys(name, key_or_data)

            # Check the type of value
            if isinstance(value, (float, int)):
                # Single value

                if N_dim > 1 and len(keys) == N_dim:
                    # Ambiguous case: ._key() above returns ['dim_0', 'dim_1'], when we
                    # really want [['dim_0', 'dim_1']]
                    keys = [keys]

                # Use the same value for all keys
                values = [float(value)] * len(keys)
            else:
                # Multiple values
                values = value

            data = pd.DataFrame(zip(keys, values), columns=["key", "value"])
            if data.isna().any(axis=None):
                raise ValueError("Length mismatch between keys and values")

        # Column types
        types = {
            "key": str if N_dim == 1 else object,
            "value": float,
            "unit": str,
            "comment": str,
        }

        # Further handle each column
        if "key" not in data.columns:
            # Form the 'key' column from other columns
            if N_dim > 1 and len(data):
                data["key"] = data.apply(
                    partial(as_str_list, idx_names=idx_names), axis=1
                )
            else:
                data["key"] = data[idx_names[0]]

        if "value" not in data.columns:
            raise ValueError("no parameter values supplied")

        if "unit" not in data.columns:
            # Broadcast single unit across all values. pandas raises ValueError
            # if *unit* is iterable but the wrong length
            data["unit"] = unit or "???"

        if "comment" not in data.columns:
            if comment:
                # Broadcast single comment across all values. pandas raises
                # ValueError if *comment* is iterable but the wrong length
                data["comment"] = comment
            else:
                # Store a 'None' comment
                data["comment"] = None
                types.pop("comment")

        # Convert types, generate tuples
        elements = map(
            lambda e: (e.key, e.value, e.unit, e.comment),
            data.astype(types).itertuples(),
        )

        # Store
        self._backend("item_set_elements", "par", name, elements)

    def init_scalar(self, name, val, unit, comment=None):
        """Initialize a new scalar.

        Parameters
        ----------
        name : str
            Name of the scalar
        val : number
            Initial value of the scalar.
        unit : str
            Unit of the scalar.
        comment : str, optional
            Description of the scalar.
        """
        self.init_par(name, [], [])
        self.change_scalar(name, val, unit, comment)

    def scalar(self, name: str):
        """Return the value and unit of a scalar.

        Parameters
        ----------
        name : str
            Name of the scalar.

        Returns
        -------
        {'value': value, 'unit': unit}
        """
        return self._backend("item_get_elements", "par", name, None)

    def change_scalar(self, name, val, unit, comment=None):
        """Set the value and unit of a scalar.

        Parameters
        ----------
        name : str
            Name of the scalar.
        val : number
            New value of the scalar.
        unit : str
            New unit of the scalar.
        comment : str, optional
            Description of the change.
        """
        self._backend(
            "item_set_elements", "par", name, [(None, float(val), unit, comment)]
        )

    def remove_par(self, name, key=None):
        """Remove parameter values or an entire parameter.

        Parameters
        ----------
        name : str
            Name of the parameter.
        key : dataframe or key list or concatenated string, optional
            Elements to be removed
        """
        if key is None:
            self._backend("delete_item", "par", name)
        else:
            self._backend("item_delete_elements", "par", name, self._keys(name, key))

    def var_list(self) -> List[str]:
        """List all defined variables."""
        return self._backend("list_items", "var")

    def has_var(self, name: str) -> bool:
        """Check whether the scenario has a variable with that name."""
        return name in self.var_list()

    def init_var(
        self, name: str, idx_sets: Sequence[str] = None, idx_names: Sequence[str] = None
    ) -> None:
        """Initialize a new variable.

        Parameters
        ----------
        name : str
            Name of the variable.
        idx_sets : sequence of str or str, optional
            Name(s) of index sets for a 1+-dimensional variable.
        idx_names : sequence of str or str, optional
            Names of the dimensions indexed by `idx_sets`.
        """
        idx_sets = as_str_list(idx_sets) or []
        idx_names = as_str_list(idx_names)
        return self._backend("init_item", "var", name, idx_sets, idx_names)

    def var(self, name, filters=None, **kwargs):
        """Return a dataframe of (filtered) elements for a specific variable.

        Parameters
        ----------
        name : str
            name of the variable
        filters : dict
            index names mapped list of index set elements
        """
        return self._backend("item_get_elements", "var", name, filters)

    def equ_list(self):
        """List all defined equations."""
        return self._backend("list_items", "equ")

    def init_equ(self, name, idx_sets=None, idx_names=None):
        """Initialize a new equation.

        Parameters
        ----------
        name : str
            Name of the equation.
        idx_sets : sequence of str or str, optional
            Name(s) of index sets for a 1+-dimensional variable.
        idx_names : sequence of str or str, optional
            Names of the dimensions indexed by `idx_sets`.
        """
        idx_sets = as_str_list(idx_sets) or []
        idx_names = as_str_list(idx_names)
        return self._backend("init_item", "equ", name, idx_sets, idx_names)

    def has_equ(self, name: str) -> bool:
        """Check whether the scenario has an equation with that name."""
        return name in self.equ_list()

    def equ(self, name, filters=None, **kwargs):
        """Return a dataframe of (filtered) elements for a specific equation.

        Parameters
        ----------
        name : str
            name of the equation
        filters : dict
            index names mapped list of index set elements
        """
        return self._backend("item_get_elements", "equ", name, filters)

    def clone(
        self,
        model: str = None,
        scenario: str = None,
        annotation: str = None,
        keep_solution: bool = True,
        shift_first_model_year: int = None,
        platform: Platform = None,
    ) -> "Scenario":
        """Clone the current scenario and return the clone.

        If the (`model`, `scenario`) given already exist on the :class:`.Platform`, the
        `version` for the cloned Scenario follows the last existing version. Otherwise,
        the `version` for the cloned Scenario is 1.

        .. note::
            :meth:`clone` does not set or alter default versions. This means that a
            clone to new (`model`, `scenario`) names has no default version, and will
            not be returned by :meth:`Platform.scenario_list` unless `default=False` is
            given.

        Parameters
        ----------
        model : str, optional
            New model name. If not given, use the existing model name.
        scenario : str, optional
            New scenario name. If not given, use the existing scenario name.
        annotation : str, optional
            Explanatory comment for the clone commit message to the database.
        keep_solution : bool, optional
            If :py:const:`True`, include all timeseries data and the solution (vars and
            equs) from the source scenario in the clone. If :py:const:`False`, only
            include timeseries data marked `meta=True` (see :meth:`.add_timeseries`).
        shift_first_model_year: int, optional
            If given, all timeseries data in the Scenario is omitted from the clone for
            years from `first_model_year` onwards. Timeseries data with the `meta` flag
            (see :meth:`.add_timeseries`) are cloned for all years.
        platform : :class:`Platform`, optional
            Platform to clone to (default: current platform)
        """
        if shift_first_model_year is not None:
            if keep_solution:
                log.warning("Override keep_solution=True for shift_first_model_year")
                keep_solution = False

        platform = platform or self.platform
        model = model or self.model
        scenario = scenario or self.scenario

        args = [platform, model, scenario, annotation, keep_solution]
        if check_year(shift_first_model_year, "first_model_year"):
            args.append(shift_first_model_year)

        return self._backend("clone", *args)

    def has_solution(self) -> bool:
        """Return :obj:`True` if the Scenario contains model solution data."""
        return self._backend("has_solution")

    def remove_solution(self, first_model_year: int = None):
        """Remove the solution from the scenario.

        This function removes the solution (variables and equations) and timeseries
        data marked as `meta=False` from the scenario (see :meth:`.add_timeseries`).

        Parameters
        ----------
        first_model_year: int, optional
            If given, timeseries data marked as `meta=False` is removed only for years
            from `first_model_year` onwards.

        Raises
        ------
        ValueError
            If Scenario has no solution or if `first_model_year` is not `int`.
        """
        if self.has_solution():
            check_year(first_model_year, "first_model_year")
            self._backend("clear_solution", first_model_year)
        else:
            raise ValueError("This Scenario does not have a solution!")

    def solve(
        self,
        model: str = None,
        callback: Callable = None,
        cb_kwargs: Dict[str, Any] = {},
        **model_options,
    ) -> None:
        """Solve the model and store output.

        ixmp 'solves' a model by invoking the run() method of a :class:`.Model`
        subclass—for instance, :meth:`.GAMSModel.run`. Depending on the underlying
        model code, different steps are taken; see each model class for details. In
        general:

        1. Data from the Scenario are written to a **model input file**.
        2. Code or an external program is invoked to perform calculations or
           optimizations, **solving the model**.
        3. Data representing the model outputs or solution are read from a **model
           output file** and stored in the Scenario.

        If the optional argument `callback` is given, additional steps are performed:

        4. Execute the `callback` with the Scenario as an argument. The Scenario has an
           `iteration` attribute that stores the number of times the underlying model
           has been solved (#2).
        5. If the `callback` returns :obj:`False` or similar, iterate by repeating from
           step #1. Otherwise, exit.

        Parameters
        ----------
        model : str
            model (e.g., MESSAGE) or GAMS file name (excluding '.gms')
        callback : callable, optional
            Method to execute arbitrary non-model code. Must accept a single argument:
            the Scenario. Must return a non-:obj:`False` value to indicate convergence.
        cb_kwargs : dict, optional
            Keyword arguments to pass to `callback`.
        model_options :
            Keyword arguments specific to the `model`. See :class:`.GAMSModel`.

        Warns
        -----
        UserWarning
            If `callback` is given and returns :obj:`None`. This may indicate that the
            user has forgotten a ``return`` statement, in which case the iteration will
            continue indefinitely.

        Raises
        ------
        ValueError
            If the Scenario has already been solved.
        """
        if self.has_solution():
            raise ValueError(
                "Scenario contains a model solution; call .remove_solution() before "
                "solve()"
            )

        # Instantiate a model
        model_obj = get_model(model or self.scheme, **model_options)

        # Validate `callback`
        if callback is not None:
            if not callable(callback):
                raise ValueError(f"callback={repr(callback)} is not callable")
            cb = callback
        else:

            def cb(scenario, **kwargs):
                return True

        # Flag to warn if the *callback* appears not to return anything
        warn_none = True

        # Iterate until convergence
        while True:
            model_obj.run(self)

            # Store an iteration number to help the callback
            if not hasattr(self, "iteration"):
                self.iteration = 0

            self.iteration += 1

            # Invoke the callback
            cb_result = cb(self, **cb_kwargs)

            if cb_result is None and warn_none:
                warn(
                    "solve(callback=...) argument returned None; will loop "
                    "indefinitely unless True is returned."
                )
                # Don't repeat the warning
                warn_none = False

            if cb_result:
                # Callback indicates convergence is reached
                break

    def get_meta(self, name: str = None):
        """Get scenario meta.

        Parameters
        ----------
        name : str, optional
            meta category name
        """
        all_meta = self.platform._backend.get_meta(
            self.model, self.scenario, self.version
        )
        return all_meta[name] if name else all_meta

    def set_meta(self, name_or_dict: Union[str, Dict[str, Any]], value=None) -> None:
        """Set scenario meta.

        Parameters
        ----------
        name_or_dict : str or dict
            If the argument is dict, it used as a mapping of meta categories (names) to
            values. Otherwise, use the argument as the meta category name.
        value : str or number or bool, optional
            Meta category value.
        """
        if isinstance(name_or_dict, str):
            name_or_dict = {name_or_dict: value}
        elif not isinstance(name_or_dict, dict):
            raise TypeError(
                f"name_or_dict must be str or dict; got {type(name_or_dict)}"
            )
        self.platform._backend.set_meta(
            name_or_dict, self.model, self.scenario, self.version
        )

    def delete_meta(self, *args, **kwargs) -> None:
        """Remove scenario meta.

        .. deprecated:: 3.1

           Use :meth:`remove_meta()`.

        Parameters
        ----------
        name : str or list of str
            Either single meta key or list of keys.
        """
        warn("Scenario.delete_meta(); use remove_meta()", DeprecationWarning)
        self.remove_meta(*args, **kwargs)

    def remove_meta(self, name: Union[str, Sequence[str]]) -> None:
        """Remove scenario meta.

        Parameters
        ----------
        name : str or list of str
            Either single meta key or list of keys.
        """
        self.platform._backend.remove_meta(
            as_str_list(name), self.model, self.scenario, self.version
        )

    # Input and output
    def to_excel(
        self,
        path: PathLike,
        items: ItemType = ItemType.SET | ItemType.PAR,
        filters: Dict[str, Union[Sequence[str], "Scenario"]] = None,
        max_row: int = None,
    ) -> None:
        """Write Scenario to a Microsoft Excel file.

        Parameters
        ----------
        path : os.PathLike
            File to write. Must have suffix :file:`.xlsx`.
        items : ItemType, optional
            Types of items to write. Either :attr:`.SET` | :attr:`.PAR` (i.e. only sets
            and parameters), or :attr:`.MODEL` (also variables and equations, i.e.
            model solution data).
        filters : dict, optional
            Filters for values along dimensions; same as the `filters` argument to
            :meth:`par`.
        max_row: int, optional
            Maximum number of rows in each sheet. If the number of elements in an item
            exceeds this number or :data:`.EXCEL_MAX_ROWS`, then an item is written to
            multiple sheets named, e.g. 'foo', 'foo(2)', 'foo(3)', etc.

        See also
        --------
        :ref:`excel-data-format`
        read_excel
        """
        # Default filters: empty dict
        filters = filters or dict()

        # Select the current scenario
        filters["scenario"] = self

        # Invoke the backend method
        self.platform._backend.write_file(
            Path(path), items, filters=filters, max_row=max_row
        )

    def read_excel(
        self,
        path: PathLike,
        add_units: bool = False,
        init_items: bool = False,
        commit_steps: bool = False,
    ) -> None:
        """Read a Microsoft Excel file into the Scenario.

        Parameters
        ----------
        path : os.PathLike
            File to read. Must have suffix '.xlsx'.
        add_units : bool, optional
            Add missing units, if any, to the Platform instance.
        init_items : bool, optional
            Initialize sets and parameters that do not already exist in the
            Scenario.
        commit_steps : bool, optional
            Commit changes after every data addition.

        See also
        --------
        :ref:`excel-data-format`
        .TimeSeries.read_file
        to_excel
        """
        self.platform._backend.read_file(
            Path(path),
            ItemType.MODEL,
            filters=dict(scenario=self),
            add_units=add_units,
            init_items=init_items,
            commit_steps=commit_steps,
        )

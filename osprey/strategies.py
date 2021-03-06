from __future__ import print_function, absolute_import, division
import sys
import inspect
import socket

import numpy as np
from sklearn.utils import check_random_state
from sklearn.model_selection import ParameterGrid
import math
try:
    from hyperopt import (Trials, tpe, fmin, STATUS_OK, STATUS_RUNNING,
                          STATUS_FAIL)
except ImportError:
    # hyperopt is optional, but required for hyperopt_tpe()
    pass

try:
    from GPy import kern
    from GPy.kern import RBF, Fixed, Bias
    from GPy.util.linalg import tdot
    from GPy.models import GPRegression
    from scipy.optimize import minimize
    from scipy.stats import norm
    # If the GPy modules fail we won't do this unnecessarily.
    from .entry_point import load_entry_point
    KERNEL_BASE_CLASS = kern.src.kern.Kern
except:
    # GPy is optional, but required for gp
    GPRegression = kern = minimize = None
    pass
from .search_space import EnumVariable

try:
    from SALib.sample import sobol_sequence as ss
except:
    ss = None
    pass

DEFAULT_TIMEOUT = socket._GLOBAL_DEFAULT_TIMEOUT


class BaseStrategy(object):
    short_name = None

    def suggest(self, history, searchspace):
        """
        Parameters
        ----------
        history : list of 3-tuples
            History of past function evaluations. Each element in history
            should be a tuple `(params, score, status)`, where `params` is a
            dict mapping parameter names to values
        searchspace : SearchSpace
            Instance of search_space.SearchSpace
        random_state :i nteger or numpy.RandomState, optional
            The random seed for sampling. If an integer is given, it fixes the
            seed. Defaults to the global numpy random number generator.

        Returns
        -------
        new_params : dict
        """
        raise NotImplementedError()

    @staticmethod
    def is_repeated_suggestion(params, history):
        """
        Parameters
        ----------
        params : dict
            Trial param set
        history : list of 3-tuples
            History of past function evaluations. Each element in history
            should be a tuple `(params, score, status)`, where `params` is a
            dict mapping parameter names to values

        Returns
        -------
        is_repeated_suggestion : bool
        """
        if any(params == hparams and hstatus == 'SUCCEEDED' for hparams, hscore, hstatus in history):
            return True
        else:
            return False


class SobolSearch(BaseStrategy):
    short_name = 'sobol'
    _SKIP = int(1e4)

    def __init__(self, length=1000):
        #TODO length should be n_trials.  But this doesn't seem to be accessible to strategies without major re-write.
        self.sequence = None
        self.length = length
        self.n_dims = 0
        self.offset = 0
        self.counter = 0

    def _set_sequence(self):
        #TODO could get rid of first part of sequence
        self.sequence = ss.sample(self.length + self._SKIP, self.n_dims)

    def _from_unit_cube(self, result, searchspace):
        # TODO this should be a method common to both Sobol and GP.
        # Note that Sobol only deals with float-valued variables, so we have
        # a transform step on either side, where int and enum valued variables
        # are transformed before calling gp, and then the result suggested by
        # Sobol needs to be reverse-transformed.
        out = {}
        for gpvalue, var in zip(result, searchspace):
            out[var.name] = var.point_from_gp(float(gpvalue))
        return out

    def suggest(self, history, searchspace):
        if 'SALib' not in sys.modules:
            raise ImportError('No module named SALib')

        if self.sequence is None:
            self.n_dims = searchspace.n_dims
            self.offset = len(history) + self._SKIP
            self._set_sequence()
        try:
            points = self.sequence[self.offset+ self.counter]
            self.counter += 1
        except IndexError:
            raise RuntimeError('Increase sobol sequence length')

        return self._from_unit_cube(points, searchspace)


class RandomSearch(BaseStrategy):
    short_name = 'random'

    def __init__(self, seed=None):
        self.seed = seed

    def suggest(self, history, searchspace):
        """Randomly suggest params from searchspace.
        """
        return searchspace.rvs(self.seed)


class HyperoptTPE(BaseStrategy):
    short_name = 'hyperopt_tpe'

    def __init__(self, seed=None, gamma=0.25, seeds=20):
        self.seed = seed
        self.gamma = gamma
        self.seeds = seeds

    def suggest(self, history, searchspace):
        """
        Suggest params to maximize an objective function based on the
        function evaluation history using a tree of Parzen estimators (TPE),
        as implemented in the hyperopt package.

        Use of this function requires that hyperopt be installed.
        """
        # This function is very odd, because as far as I can tell there's
        # no real documented API for any of the internals of hyperopt. Its
        # execution model is that hyperopt calls your objective function
        # (instead of merely providing you with suggested points, and then
        # you calling the function yourself), and its very tricky (for me)
        # to use the internal hyperopt data structures to get these predictions
        # out directly.

        # so they path we take in this function is to construct a synthetic
        # hyperopt.Trials database which from the `history`, and then call
        # hyoperopt.fmin with a dummy objective function that logs the value
        # used, and then return that value to our client.

        # The form of the hyperopt.Trials database isn't really documented in
        # the code -- most of this comes from reverse engineering it, by
        # running fmin() on a simple function and then inspecting the form of
        # the resulting trials object.
        if 'hyperopt' not in sys.modules:
            raise ImportError('No module named hyperopt')

        random = check_random_state(self.seed)
        hp_searchspace = searchspace.to_hyperopt()

        trials = Trials()
        for i, (params, scores, status) in enumerate(history):
            if status == 'SUCCEEDED':
                # we're doing maximization, hyperopt.fmin() does minimization,
                # so we need to swap the sign
                result = {'loss': -np.mean(scores), 'status': STATUS_OK}
            elif status == 'PENDING':
                result = {'status': STATUS_RUNNING}
            elif status == 'FAILED':
                result = {'status': STATUS_FAIL}
            else:
                raise RuntimeError('unrecognized status: %s' % status)

            # the vals key in the trials dict is basically just the params
            # dict, but enum variables (hyperopt hp.choice() nodes) are
            # different, because the index of the parameter is specified
            # in vals, not the parameter itself.

            vals = {}
            for var in searchspace:
                if isinstance(var, EnumVariable):
                    # get the index in the choices of the parameter, and use
                    # that.
                    matches = [i for i, c in enumerate(var.choices)
                               if c == params[var.name]]
                    assert len(matches) == 1
                    vals[var.name] = matches
                else:
                    # the other big difference is that all of the param values
                    # are wrapped in length-1 lists.
                    vals[var.name] = [params[var.name]]

            trials.insert_trial_doc({
                'misc': {
                    'cmd': ('domain_attachment', 'FMinIter_Domain'),
                    'idxs': dict((k, [i]) for k in hp_searchspace.keys()),
                    'tid': i,
                    'vals': vals,
                    'workdir': None},
                'result': result,
                'tid': i,
                # bunch of fixed fields that hyperopt seems to require
                'owner': None, 'spec': None, 'state': 2, 'book_time': None,
                'exp_key': None, 'refresh_time': None, 'version': 0
                })

        trials.refresh()
        chosen_params_container = []

        def suggest(*args, **kwargs):
            return tpe.suggest(*args, **kwargs, gamma=self.gamma, n_startup_jobs=self.seeds)

        def mock_fn(x):
            # http://stackoverflow.com/a/3190783/1079728
            # to get around no nonlocal keywork in python2
            chosen_params_container.append(x)
            return 0

        fmin(fn=mock_fn, algo=suggest, space=hp_searchspace, trials=trials,
             max_evals=len(trials.trials)+1,
             **self._hyperopt_fmin_random_kwarg(random))
        chosen_params = chosen_params_container[0]

        return chosen_params



    @staticmethod
    def _hyperopt_fmin_random_kwarg(random):
        if 'rstate' in inspect.getargspec(fmin).args:
            # 0.0.3-dev version uses this argument
            kwargs = {'rstate': random, 'allow_trials_fmin': False}
        elif 'rseed' in inspect.getargspec(fmin).args:
            # 0.0.2 version uses different argument
            kwargs = {'rseed': random.randint(2**32-1)}
        return kwargs


class GP(BaseStrategy):
    short_name = 'gp'

    def __init__(self, kernels=None, acquisition=None, seed=None, seeds=1, n_iter=50, 
            n_init = 20, sobol_init=False, optimize_best=False, max_iter=1E5,
            predict_from_gp=True):
        self.seed = seed
        self.seeds = seeds
        self.max_iter = int(max_iter)
        self.n_iter = int(n_iter)
        self.n_init = int(n_init)
        self.sobol_init = bool(sobol_init)
        self.optimize_best = bool(optimize_best)
        self.predict_from_gp = bool(predict_from_gp)
        self.model = None
        self.n_dims = None
        self.kernel = None
        self.x_best = None
        self.y_best = None
        self.transformed = False
        if kernels is None:
            kernels = [{'name': 'GPy.kern.Matern52', 'params': {'ARD': True},
                        'options': {'independent': False}}]
        self._kerns = kernels
        if acquisition is None:
            acquisition = {'name': 'osprey', 'params': {}}
        self.acquisition_function = acquisition
        self._acquisition_function = None
        self._set_acquisition()

    def _create_kernel(self):
        # Check kernels
        kernels = self._kerns
        if not isinstance(kernels, list):
            raise RuntimeError('Must provide enumeration of kernels')
        for kernel in kernels:
            if sorted(list(kernel.keys())) != ['name', 'options', 'params']:
                raise RuntimeError(
                    'strategy/params/kernels must contain keys: "name", "options", "params"')

        # Turn into entry points.
        # TODO use eval to allow user to specify internal variables for kernels (e.g. V) in config file.
        kernels = []
        for kern in self._kerns:
            params = kern['params']
            options = kern['options']
            name = kern['name']
            kernel_ep = load_entry_point(name, 'strategy/params/kernels')
            if issubclass(kernel_ep, KERNEL_BASE_CLASS):
                if options['independent']:
                    #TODO Catch errors here?  Estimator entry points don't catch instantiation errors
                    kernel = np.sum([kernel_ep(1, active_dims=[i], **params) for i in range(self.n_dims)])
                else:
                    kernel = kernel_ep(self.n_dims, **params)
            if not isinstance(kernel, KERNEL_BASE_CLASS):
                raise RuntimeError('strategy/params/kernel must load a'
                                   'GPy derived Kernel')
            kernels.append(kernel)
        self.kernel = np.sum(kernels)

    def _fit_model(self, X, Y):
        if max(Y) < 0:
            self.transformed = True
        else:
            self.transformed = False

        Y_trans = self._transform_score(Y)

        model = GPRegression(X, Y_trans, self.kernel)
        # Catch fitting error
        try:
            model.optimize_restarts(num_restarts=self.n_init, verbose=False)
            self.model = model
        except np.linalg.linalg.LinAlgError:
            self.model = None

    def _transform_score(self, Y):
        if self.transformed:
            return -np.log(-Y)
        return Y

    def _back_transform_score(self, Y):
        if self.transformed:
            return - np.exp(-Y)
        return Y

    def _get_init(self):
        if self.sobol_init:
            return self._get_sobol_points()
        return self._get_random_points()

    def _get_random_points(self):
        return np.random.random((self.n_iter, self.n_dims))

    def _get_sobol_points(self):
        return ss.sample(self.n_iter + np.random.randint(1000), self.n_dims)[-self.n_iter:]

    def _is_var_positive(self, var):

        if np.any(var < 0):
            # RuntimeError may be overkill
            raise RuntimeError('Negative variance predicted from regression model.')
        else:
            return True

    #def _ei(self, x, y_mean, y_var):
    #    y_std = np.sqrt(y_var)
    #    z = (y_mean - self._transform_score(self.y_best))/y_std
    #    result = y_std*(z*norm.cdf(z) + norm.pdf(z))
    #    return result

    def _ei(self, x, y_mean, y_var, kappa=0.01):
        y_std = np.sqrt(y_var + self.y_best_var)
        z = (y_mean - self._transform_score(self.y_best) - kappa)/y_std
        result = y_std*(z*norm.cdf(z) + norm.pdf(z))
        return result

    def _ucb(self, x, y_mean, y_var, kappa=1.0):
        result = y_mean + kappa*np.sqrt(y_var + self.y_best_var)
        return result

    def _osprey(self, x, y_mean, y_var):
        return (y_mean+y_var).flatten()

    def get_gp_best(self):
        # Objective function
        def z(x):
            X = x.reshape(-1, self.n_dims)
            y_mean, y_var = self.model.predict(X)

            return -y_mean

        best_observation = self.model.X[self.model.Y.argmax(axis=0)].flatten()

        res = minimize(z, best_observation, bounds=self.n_dims*[(0., 1.)],
                        options={'maxiter': self.max_iter, 'disp': 0})

        return res.x

    def _optimize_acquisition(self):
        # Objective function
        def z(x):
            # TODO make spread of points around x and take mean value.
            X = x.reshape(-1, self.n_dims)
            y_mean, y_var = self.model.predict(X)
            # This code is for debug/testing phase only.
            # Ideally we should test for negative variance regardless of the AF.
            # However, we want to recover the original functionality of Osprey, hence the conditional block.
            # TODO remove this.
            if self.acquisition_function['name'] in ['osprey', 'ucb']:
                af = self._acquisition_function(X, y_mean=y_mean, y_var=y_var)
            else:
                if self._is_var_positive(y_var):
                    af = self._acquisition_function(X, y_mean=y_mean, y_var=y_var)
                else:
                    return 0

            return (-1)*af

        init_tries = self._get_init()

        # Optimization loop
        acquisition_fns = []
        candidates = []
        for i in range(self.n_iter):
            init = init_tries[i]
            if self.max_iter > 0:
                res = minimize(z, init, bounds=self.n_dims*[(0., 1.)],
                                options={'maxiter': self.max_iter, 'disp': 0})
                candidates.append(res.x)
                acquisition_fns.append(res.fun)
            else:
                candidates.append(init)
                acquisition_fns.append(z(init))

        # Choose the best
        acquisition_fns = np.array(acquisition_fns).flatten()
        candidates = np.array(candidates)
        best_index = int(np.argmin(acquisition_fns))
        best_candidate = candidates[best_index]
        return best_candidate

    def _set_acquisition(self):
        if isinstance(self.acquisition_function, list):
            raise RuntimeError('Must specify only one acquisition function')
        if sorted(self.acquisition_function.keys()) != ['name', 'params']:
            raise RuntimeError('strategy/params/acquisition must contain keys '
                               '"name" and "params"')
        if self.acquisition_function['name'] not in ['ei', 'ucb', 'osprey']:
            raise RuntimeError('strategy/params/acquisition name must be one of '
                               '"ei", "ucb", "osprey"')

        if 'params' in self.acquisition_function \
                and 'kappa' in self.acquisition_function['params']:
            self.acquisition_function['params']['kappa'] = \
                    float(self.acquisition_function['params']['kappa'])

        f = eval('self._'+self.acquisition_function['name'])

        def g(x, y_mean, y_var):
            return f(x, y_mean, y_var, **self.acquisition_function['params'])

        self._acquisition_function = g

    def _get_data(self, history, searchspace):
        X = []
        Y = []
        V = []
        ignore = []
        for param_dict, scores, status in history:
            # transform points into the GP domain. This invloves bringing
            # int and enum variables to floating point, etc.
            if status == 'FAILED':
                # not sure how to deal with these yet
                continue

            point = searchspace.point_to_gp(param_dict)
            if status == 'SUCCEEDED':
                X.append(point)
                Y.append(np.mean(scores))
                V.append(np.var(scores))

            elif status == 'PENDING':
                ignore.append(point)
            else:
                raise RuntimeError('unrecognized status: %s' % status)

        return (np.array(X).reshape(-1, self.n_dims),
                np.array(Y).reshape(-1, 1),
                np.array(V).reshape(-1, 1),
                np.array(ignore).reshape(-1, self.n_dims))

    def _from_gp(self, result, searchspace):

        # Note that GP only deals with float-valued variables, so we have
        # a transform step on either side, where int and enum valued variables
        # are transformed before calling gp, and then the result suggested by
        # GP needs to be reverse-transformed.
        out = {}
        for gpvalue, var in zip(result, searchspace):
            out[var.name] = var.point_from_gp(float(gpvalue))

        return out

    def _is_within(self, point, X, tol=1E-2):
        if True in (np.sqrt(((point - X)**2).sum(axis=0)) <= tol):
            return True
        return False

    def suggest(self, history, searchspace, max_tries=5):
        if not GPRegression:
            raise ImportError('No module named GPy')
        if not minimize:
            raise ImportError('No module named SciPy')

        if len(history) < self.seeds:
            return RandomSearch().suggest(history, searchspace)

        self.n_dims = searchspace.n_dims

        X, Y, V, ignore = self._get_data(history, searchspace)

        if len(Y) < self.seeds:
            return RandomSearch().suggest(history, searchspace)

        # TODO make _create_kernel accept optional args.
        self._create_kernel()
        self._fit_model(X, Y)

        # Catch fitting error
        if self.model is None:
            return RandomSearch().suggest(history, searchspace)

        if self.optimize_best:
            x_best = self.get_gp_best()
            y_best, self.y_best_var = self.model.predict(x_best.reshape(-1, self.n_dims))
        else:
            best_idx = self.model.Y.argmax(axis=0)
            x_best = self.model.X[best_idx].flatten()
            if self.predict_from_gp:
                y_best, self.y_best_var = self.model.predict(x_best.reshape(-1, self.n_dims))
            else:
                y_best = self.model.Y[best_idx].flatten()[0]
                self.y_best_var = 0
        self.y_best = self._back_transform_score(y_best)
        self.x_best = self._from_gp(x_best, searchspace)

        suggestion = self._optimize_acquisition()

        if suggestion in ignore or self._is_within(suggestion, X):
            return RandomSearch().suggest(history, searchspace)

        return self._from_gp(suggestion, searchspace)


class GridSearch(BaseStrategy):
    short_name = 'grid'

    def __init__(self):
        self.param_grid = None
        self.current = -1

    def suggest(self, history, searchspace):
        # Convert searchspace to param_grid
        if self.param_grid is None:
            if not all(isinstance(v, EnumVariable) for v in searchspace):
                raise RuntimeError("GridSearchStrategy is defined only for all-enum search space")

            self.param_grid = ParameterGrid(dict((v.name, v.choices) for v in searchspace))

        # NOTE: there is no way of signaling end of parameters to be searched against
        # so user should pick correctly number of evaluations
        self.current += 1
        return self.param_grid[self.current % len(self.param_grid)]

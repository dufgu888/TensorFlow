"""
This module defines the structures for different models, including
CellBox, linear regression, Neural network, and co-expression
"""

import numpy as np
import tensorflow as tf
import cellbox.kernel
from cellbox.utils import loss, optimize
import tensorflow._api.v2.compat.v1 as tf_v1
# import tensorflow_probability as tfp

def factory(args):
    """define model type based on configuration input"""
    if args.model == 'CellBox':
        return CellBox(args).build()
    # Deprecated for now, use scikit-learn instead
    # TODO: update the co-expression models
    if args.model == 'CoExp':
        return CoExp(args).build()
    if args.model == 'CoExp_nonlinear':
        return CoExpNonlinear(args).build()
    if args.model == 'LinReg':
        return LinReg(args).build()
    if args.model == 'NN':
        return NN(args).build()
    # TODO: baysian model
    # if args.model == 'Bayesian':
    #     return BN(args).build()


class PertBio:
    """define abstract perturbation model"""
    def __init__(self, args):
        self.args = args
        self.n_x = args.n_x
        self.pert_in, self.expr_out = args.pert_in, args.expr_out
        self.iter_train, self.iter_monitor, self.iter_eval = args.iter_train, args.iter_monitor, args.iter_eval
        self.train_x, self.train_y = self.iter_train.get_next()
        self.monitor_x, self.monitor_y = self.iter_monitor.get_next()
        self.eval_x, self.eval_y = self.iter_eval.get_next()
        self.l1_lambda, self.l2_lambda = self.args.l1_lambda_placeholder, self.args.l2_lambda_placeholder
        self.lr = self.args.lr

    def get_ops(self):
        """get operators for tensorflow"""
        if self.args.weight_loss == 'expr':
            self.train_loss, self.train_mse_loss = loss(self.train_y, self.train_yhat, self.params['W'],
                                                        self.l1_lambda, self.l2_lambda, weight=self.train_y, alpha = 0.5)
            self.monitor_loss, self.monitor_mse_loss = loss(self.monitor_y, self.monitor_yhat, self.params['W'],
                                                            self.l1_lambda, self.l2_lambda, weight=self.monitor_y, alpha = 0.5)
            self.eval_loss, self.eval_mse_loss = loss(self.eval_y, self.eval_yhat, self.params['W'],
                                                      self.l1_lambda, self.l2_lambda, weight=self.eval_y, alpha = 0.5)
        elif self.args.weight_loss == 'None':
            self.train_loss, self.train_mse_loss = loss(self.train_y, self.train_yhat, self.params['W'],
                                                        self.l1_lambda, self.l2_lambda, alpha = 0.5)
            self.monitor_loss, self.monitor_mse_loss = loss(self.monitor_y, self.monitor_yhat, self.params['W'],
                                                            self.l1_lambda, self.l2_lambda, alpha = 0.5)
            self.eval_loss, self.eval_mse_loss = loss(self.eval_y, self.eval_yhat, self.params['W'],
                                                      self.l1_lambda, self.l2_lambda, alpha = 0.5)
        self.op_optimize = optimize(self.train_loss, self.lr)

    def get_variables(self):
        """get model parameters (overwritten by model configuration)"""
        raise NotImplementedError

    def forward(self, x, mu):
        """forward propagation (overwritten by model configuration)"""
        raise NotImplementedError
    
    def build(self):
        """build model"""
        self.params = {}
        self.get_variables()
        self.train_yhat = self.forward(self.train_y0, self.train_x)
        self.monitor_yhat = self.forward(self.monitor_y0, self.monitor_x)
        self.eval_yhat = self.forward(self.eval_y0, self.train_x)
        self.get_ops()
        return self


class CellBox(PertBio):
    """CellBox model"""
    def build(self):
        self.params = {}
        self.get_variables()
        if self.args.pert_form == 'by u':
            y0 = tf.constant(np.zeros((self.n_x, 1)), name="x_init", dtype=tf.float32)
            self.train_y0 = y0
            self.monitor_y0 = y0
            self.eval_y0 = y0
            self.gradient_zero_from = None
        elif self.args.pert_form == 'fix x':  # fix level of node x (here y) by input perturbation u (here x)
            self.train_y0 = tf.transpose(self.train_x)
            self.monitor_y0 = tf.transpose(self.monitor_x)
            self.eval_y0 = tf.transpose(self.eval_x)
            self.gradient_zero_from = self.args.n_activity_nodes
        self.envelope_fn = cellbox.kernel.get_envelope(self.args)
        self.ode_solver = cellbox.kernel.get_ode_solver(self.args)
        self._dxdt = cellbox.kernel.get_dxdt(self.args, self.params)
        self.convergence_metric_train, self.train_yhat = self.forward(self.train_y0, self.train_x)
        self.convergence_metric_monitor, self.monitor_yhat = self.forward(self.monitor_y0, self.monitor_x)
        self.convergence_metric_eval, self.eval_yhat = self.forward(self.eval_y0, self.eval_x)
        self.get_ops()
        return self

    def forward(self, y0, mu):
        if isinstance(mu, tf.SparseTensor):
            mu_t = tf.sparse.to_dense(tf.sparse.transpose(mu))
        else:
            mu_t = tf.transpose(mu)
        ys = self.ode_solver(y0, mu_t, self.args.dT, self.args.n_T, self._dxdt, self.gradient_zero_from)
        # [n_T, n_x, batch_size]
        ys = ys[-self.args.ode_last_steps:]
        # [n_iter_tail, n_x, batch_size]
        mean, sd = tf.nn.moments(ys, axes=0)
        yhat = tf.transpose(ys[-1])
        dxdt = self._dxdt(ys[-1], mu_t)
        # [n_x, batch_size] for last ODE step
        convergence_metric = tf.concat([mean, sd, dxdt], axis=0)
        return convergence_metric, yhat

    def get_variables(self):
        """
        Initialize parameters in the Hopfield equation

        Mutates:
            self.params(dict):{
                W (tf.Variable): interaction matrix with constraints enforced, , shape: [n_x, n_x]
                alpha (tf.Variable): alpha, shape: [n_x, 1]
                eps (tf.Variable): eps, shape: [n_x, 1]
            }
        """
        n_x, n_protein_nodes, n_activity_nodes = self.n_x, self.args.n_protein_nodes, self.args.n_activity_nodes
        with tf_v1.variable_scope("initialization", reuse=True):
            """
               Enforce constraints  (i: recipient)
               no self regulation wii=0
               ingoing wij for drug nodes (88th to 99th) = 0 [n_activity_nodes 87: ]
                                w [87:99,_] = 0
               outgoing wij for phenotypic nodes (83th to 87th) [n_protein_nodes 82 : n_activity_nodes 87]
                                w [_, 82:87] = 0
               ingoing wij for phenotypic nodes from drug ndoes (direct) [n_protein_nodes 82 : n_activity_nodes 87]
                                w [82:87, 87:99] = 0
            """
            W = tf.Variable(np.random.normal(0.01, size=(n_x, n_x)), name="W", dtype=tf.float32)
            W_mask = (1.0 - np.diag(np.ones([n_x])))
            W_mask[n_activity_nodes:, :] = np.zeros([n_x - n_activity_nodes, n_x])
            W_mask[:, n_protein_nodes:n_activity_nodes] = np.zeros([n_x, n_activity_nodes - n_protein_nodes])
            W_mask[n_protein_nodes:n_activity_nodes, n_activity_nodes:] = np.zeros([n_activity_nodes - n_protein_nodes,
                                                                                    n_x - n_activity_nodes])
            self.params['W'] = W_mask * W

            eps = tf.Variable(np.ones((n_x, 1)), name="eps", dtype=tf.float32)
            alpha = tf.Variable(np.ones((n_x, 1)), name="alpha", dtype=tf.float32)
            self.params['alpha'] = tf.nn.softplus(alpha)
            self.params['eps'] = tf.nn.softplus(eps)

            if self.args.envelope == 2:
                psi = tf.Variable(np.ones((n_x, 1)), name="psi", dtype=tf.float32)
                self.params['psi'] = tf.nn.softplus(psi)


class LinReg(PertBio):
    """linear regression model"""
    def get_variables(self):
        with tf_v1.variable_scope("initialization", reuse=True):
            self.params.update({
                'W': tf.Variable(np.random.normal(0.01, size=(self.n_x, self.n_x)), name="W", dtype=tf.float32),
                'b': tf.Variable(np.random.normal(0.01, size=(self.n_x, 1)), name="b", dtype=tf.float32)
            })

    def forward(self, x, mu):
        xhat = tf.matmul(mu, self.params['W']) + tf.reshape(self.params['b'], [1, -1])
        return xhat


class NN(LinReg):
    """Neural network model"""
    def get_variables(self):
        with tf_v1.variable_scope("initialization", reuse=True):
            self.params.update({
                'W_h': tf.Variable(np.random.normal(0.01, size=(self.n_x, self.args.n_hidden)), name="Wh",
                                   dtype=tf.float32),
                'b_h': tf.Variable(np.random.normal(0.01, size=(self.args.n_hidden, 1)), name="bh", dtype=tf.float32),
                'W': tf.Variable(np.random.normal(0.01, size=(self.args.n_hidden, self.n_x)), name="Wo",
                                 dtype=tf.float32),
                'b': tf.Variable(np.random.normal(0.01, size=(self.n_x, 1)), name="bo", dtype=tf.float32)
            })

    def forward(self, x, mu):
        hidden = tf.tanh(tf.matmul(mu, self.params['W_h']) + tf.reshape(self.params['b_h'], [1, -1]))
        xhat = tf.matmul(hidden, self.params['W']) + tf.reshape(self.params['b'], [1, -1])
        return xhat


def get_idx_pair(mu):
    """get perturbation position"""
    idx = np.where(mu != 0)[0]
    idx = [idx[0], idx[-1]]
    return idx


class CoExp(PertBio):
    """Co-expression model"""
    # currently deprecated, use scikit-learn to construct co-exp models until the further updates

    def __init__(self, args):
        # TODO: redesign CoExp class
        super(CoExp, self).__init__(args)
        # self.mu_full = tf.constant(self.args.dataset['pert_full'], dtype=tf.float32)
        # self.idx_full = tf.map_fn(fn=self.get_idx_pair, elems=self.mu_full, dtype=tf.int32)
        self.mu_full = self.args.dataset['pert_full'].values
        self.pos_full = [get_idx_pair(mu) for mu in self.mu_full]

    def get_variables(self):
        with tf_v1.variable_scope("initialization", reuse=True):
            Ws = tf.Variable(np.zeros([self.args.n_x, self.args.n_x, 2, self.args.n_x]), dtype=tf.float32)
            bs = tf.Variable(np.zeros([self.args.n_x, self.args.n_x, self.args.n_x]), dtype=tf.float32)
        self.params.update({'Ws': Ws, 'bs': bs})

    def _forward_unit(self, x, i, j):
        # x [2 x batch_size]
        W = self.params['Ws'][i, j]  # [2, n_x]
        b = self.params['bs'][i, j]  # [n_x]
        xij = tf.stack([x[i], x[j]])  # [2]
        xhat = tf.matmul(tf.expand_dims(xij, 0), W) + b  # [1, n_x]
        return tf.reshape(xhat[0], [-1])  # [n_x]

    def _forward_1(self, x):
        xhat_list = []
        for pos in self.pos:
            xhat_list.append(self._forward_unit(x, pos[0], pos[1]))
        xhat = tf.stack(xhat_list)  # [all_model, 99]
        return xhat

    def _forward_2(self, i, x):
        pos = self.pos[i]
        xhat = self._forward_unit(x, pos[0], pos[1])
        return xhat

    def forward(self, x_gold, training, pos=None, idx=None):
        # calculate xhats from all possible models
        if training:
            self.pos = pos
            xhats = tf.map_fn(fn=self._forward_1, elems=x_gold)
            xhats_avg = tf.reduce_mean(xhats, axis=1)
            return xhats_avg
        self.pos = tf.constant(self.pos_full)
        xhats_selected = tf.map_fn(fn=lambda elem: self._forward_2(elem[0], elem[1]), elems=(idx, x_gold),
                                   dtype=tf.float32)
        return xhats_selected

    def get_ops(self):
        self.l1_lambda = tf_v1.placeholder(tf.float32, shape=[])
        self.loss_mse = tf.reduce_mean(tf.square((self.train_x - self.xhat)))
        self.loss_mse_training = tf.reduce_mean(tf.square((self.train_x - self.xhat_training)))
        self.loss = self.loss_mse_training + self.l1_lambda * tf.reduce_mean(tf.abs(self.params['Ws']))
        self.lr = tf_v1.placeholder(tf.float32)
        self.op_optimize = optimize(self.loss, self.lr, var_list=None)

    def build(self):
        self.params = {}
        self.get_variables()
        self.xhat_training = self.forward(self.train_x, pos=self.pos_full, training=True)
        self.xhat = self.forward(self.train_x, idx=self.pos_full, training=False)
        self.get_ops()
        return self


class CoExpNonlinear(CoExp):
    """co-expression model with non-linear envelope"""
    # currently deprecated, use scikit-learn to construct co-exp models until the further updates
    def get_variables(self):
        with tf_v1.variable_scope("initialization", reuse=True):
            Ws = tf.Variable(np.zeros([self.args.n_x, self.args.n_x, self.n_x, self.n_x]), dtype=tf.float32)
            bs = tf.Variable(np.zeros([self.args.n_x, self.args.n_x, self.n_x, 1]), dtype=tf.float32)
            W = tf.Variable(np.zeros([self.n_x, 1]), dtype=tf.float32)
            b = tf.Variable(np.zeros([self.n_x, 1]), dtype=tf.float32)
        self.params.update({'Ws': Ws, 'bs': bs, 'W': W, 'b': b})

    # def forward(self, mu):
    # # during training, use mu_full, while during testing use mu
    # idx = tf.map_fn(fn=get_idx_pair, elems=mu, dtype=tf.int32)
    # # mask the models for prediction
    # Ws = tf.gather_nd(self.params['Ws'], idx)  # batch_size x [Params,]
    # bs = tf.gather_nd(self.params['bs'], idx)
    # hidden = tf.tensordot(Ws, tf.transpose(x_gold), axes=1) + bs  # batch_size x [Params,] x batch_size
    # hidden_transposed = tf.transpose(hidden, perm=[0, 2, 1])
    # hidden_masked = tf.gather_nd(hidden_transposed, tf.compat.v2.where(tf.eye(tf.shape(mu)[0])))
    # xhat = tf.matmul(tf.tanh(hidden_masked), self.params['W']) + tf.reshape(self.params['b'], [1, -1])
    # return xhat

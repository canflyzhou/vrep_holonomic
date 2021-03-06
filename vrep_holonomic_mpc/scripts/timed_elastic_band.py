#!/usr/bin/env python

import rospy
from std_msgs.msg import String
from std_msgs.msg import Float32
from geometry_msgs.msg import Twist
from geometry_msgs.msg import TransformStamped
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Odometry, Path
import math
import time
from std_srvs.srv import Empty
import numpy as np
from tf import TransformListener
from tf.transformations import euler_from_quaternion, quaternion_from_euler
from sys import path as pythonpath
try:
    casadi_path = rospy.get_param("/vrep_holonomic_mpc/casadi_path")
    pythonpath.append(casadi_path)
except:
    pass
from casadi import *



class Parameters:

    '''
    param: Symbolic variables
    ub: Upper bounds for symbolic variables
    lb: Lower bounds for symbolic variables
    add_inequality(constraints, ub, lb): Adds symbolic variables and its bounds.
    add_equality(constraints): Adds symbolic variables. Bounds of the variables are set to be 0
    '''

    def __init__(self, name=None, param=None, ub=None, lb=None):

        self.name = name
        if type(param)==type(None):
            self.param = SX.zeros(0)
            self.ub = np.array([])
            self.lb = np.array([])
        else:
            self.param = param
            if type(ub)==type(None):
                self.ub = float('inf')*np.ones(param.shape)
            else:
                self.ub = ub
            if type(lb)==type(None):
                self.lb = -float('inf')*np.ones(param.shape)
            else:
                self.lb = lb


    def __add__(self, other):

        temp = Parameters(self.name + '+' + other.name)
        temp.param = vertcat(self.param, other.param)
        temp.ub = np.concatenate([self.ub, other.ub])
        temp.lb = np.concatenate([self.lb, other.lb])

        return temp


    def __getitem__(self, key):
        return {
            'param':self.param[key],
            'ub':self.ub[key],
            'lb':self.lb[key]
        }


    def add_inequality(self, constraints, ub=None, lb=None):

        self.param = vertcat(self.param, constraints)
        if type(ub)==type(None):
            self.ub = np.concatenate([self.ub, float('inf')*np.ones([constraints.shape[0]])])
        else:
            self.ub = np.concatenate([self.ub, ub])
        if type(lb)==type(None):
            self.lb = np.concatenate([self.lb, -float('inf')*np.ones([constraints.shape[0]])])
        else:
            self.lb = np.concatenate([self.lb, lb])


    def add_equality(self, constraints):

        self.param = vertcat(self.param, constraints)
        self.ub = np.concatenate([self.ub, np.zeros([constraints.shape[0]])])
        self.lb = np.concatenate([self.lb, np.zeros([constraints.shape[0]])])



class MPC_controller:

    def __init__(self, cmd_vel_topic):

        rospy.init_node('vrep_holonomic_mpc', anonymous=True)
        time.sleep(1)
        self.tic = None
        self.toc = None
        self.prev_path = None

        self.use_odom = rospy.get_param("/vrep_holonomic_mpc/use_odom")
        self.target_sub = rospy.Subscriber("/move_base_simple/goal", PoseStamped, self.targetCallback)
        self.tfListener = TransformListener()
        self.odom_sub = rospy.Subscriber("/odom", Odometry, self.odomCallback)
        self.gx = self.gy = self.gyaw = None

        #declare velocity publisher
        self.vel_msg = Twist()
        self.vel_pub = rospy.Publisher(cmd_vel_topic, Twist, queue_size=10)
        self.path_pub = rospy.Publisher("/move_base/TebLocalPlannerROS/local_plan", Path, queue_size=10)
        self.setup_MPC()


    def spin(self, rate):
        self.rate = rospy.Rate(rate)
        rospy.spin()


    def pose_from_tf(self):

        try:
            position, quaternion = self.tfListener.lookupTransform(
                "/map",
                "/base_footprint",
                self.tfListener.getLatestCommonTime("/map", "/base_footprint")
            )
            self.curx = position[0]
            self.cury = position[1]
            (roll, pitch, yaw) = euler_from_quaternion(quaternion)
            self.curyaw = yaw
        except rospy.ROSException:
            rospy.loginfo(
                "Warn: base_footprint - No transform from [base_footprint] to [map]."
            )


    def pose_from_msg(self, msg):

        self.curx = msg.pose.pose.position.x
        self.cury = msg.pose.pose.position.y
        quat = (
            msg.pose.pose.orientation.x,
            msg.pose.pose.orientation.y,
            msg.pose.pose.orientation.z,
            msg.pose.pose.orientation.w
        )
        (roll, pitch, yaw) = euler_from_quaternion(quat)
        self.curyaw = yaw


    def odomCallback(self, msg):

        if self.use_odom:
            self.pose_from_msg(msg)
        else:
            self.pose_from_tf()

        self.publish_vel()


    def targetCallback(self, msg):

        self.gx = msg.pose.position.x
        self.gy = msg.pose.position.y
        quat = (
            msg.pose.orientation.x,
            msg.pose.orientation.y,
            msg.pose.orientation.z,
            msg.pose.orientation.w
        )
        (roll, pitch, yaw) = euler_from_quaternion(quat)
        self.gyaw = yaw

        if type(self.tic)!=type(None):
            print 'Recieved a new target pose. previous target pose will be ignored.\n'
        self.tic = rospy.get_time()
        print 'Target pose :', self.gx, self.gy, self.gyaw


    def publish_vel(self):

        if not self.use_odom:
            # Robot pose from /tf if use_odom is False
            self.pose_from_tf(self.tfListener)

        if self.compute_err() < 0.05:
            vel = [0.0, 0.0, 0.0]
            if type(self.tic)!=type(None):
                self.toc = rospy.get_time()
                print 'Time elapsed:', self.toc - self.tic, 'sec.\n'
                self.tic = None
            self.prev_path = None
        else:
            vel = self.iterate_MPC(self.N)

        self.vel_msg.linear.x  = vel[0]
        self.vel_msg.linear.y  = vel[1]
        self.vel_msg.angular.z = vel[2]
        self.vel_pub.publish(self.vel_msg)


    def compute_err(self):
        if type(self.gyaw)==type(None):
            return 0.0
        else:
            yawdiff = self.gyaw - self.curyaw
            if yawdiff > math.pi:
                yawdiff -= 2.0*math.pi
            elif yawdiff < -math.pi:
                yawdiff += 2.0*math.pi
            return np.linalg.norm(
                (self.gx - self.curx, self.gy - self.cury, yawdiff)
            )


    def setup_MPC(self):

        self.T = 0.2 # Default time horizon
        self.N = N = 20 # number of control intervals
        self.rob_diam = 0.5

        X_max = np.array([float('inf'), float('inf'), float('inf')])
        X_min = -X_max
        T_max = np.array([self.T + 0.2*self.T])
        T_min = np.array([self.T - 0.2*self.T])

        #setting up symbolic variables for states 
        x = SX.sym('x')
        y = SX.sym('y')
        theta = SX.sym('theta')
        states = vertcat(x,y,theta)
        self.n_states = states.size1()
        
        #setting up symbolic variables for control inputs 
        vX = SX.sym('vX')
        vY = SX.sym('vY')
        omega = SX.sym('omega')
        controls = vertcat(vX,vY,omega)
        self.n_controls = controls.size1()
        rhs = vertcat(vX*cos(theta)-vY*sin(theta),vX*sin(theta)+vY*cos(theta),omega) # system r.h.s

        self.f = Function('f',[states,controls],[rhs]) # nonlinear mapping function f(x,u), system equation 
        T = Parameters('T') # Decision variables (Time difference) 
        P = SX.sym('P',self.n_states + self.n_states + self.N) # parameters (which include the initial and the reference/final desired state of the robot)
        
        X = Parameters('X') # State variables

        obj = 0; # Objective function
        g = Parameters('g') # constraints

        q1 = 2
        q2 = 2
        q3 = 4

        r1 = 4
        r2 = 4
        r3 = 8
        Q = horzcat([q1,0,0],[0,q2,0],[0,0,q3])
        R = horzcat([r1,0,0],[0,r2,0],[0,0,r3])

        #setup variables for input contraints
        w_max = 7
        wheel_rad = 0.076
        a = 0.281
        b = 0.2355

        H = np.array([
            [1, -1, (-a-b)],
            [1,  1, ( a+b)],
            [1, -1, ( a+b)],
            [1,  1, (-a-b)]
        ])

        st = SX.sym('X0', self.n_states)
        X.add_inequality(st, ub=X_max, lb=X_min) # initial state
        g.add_equality(st-P[0:3]) # initial condition constraints
        # weighing matrices (controls)
        # compute objective symbollically this is what the solver will minimize 
        for k in range(0, N):
            dt = SX.sym('T'+str(k), 1)
            T.add_inequality(dt, ub=T_max, lb=T_min)
            st_next = SX.sym('X'+str(k+1), self.n_states)
            X.add_inequality(st_next, ub=X_max, lb=X_min)
            cos_theta = cos(st[2])
            sin_theta = sin(st[2])
            dx = st_next[0] - st[0]
            dy = st_next[1] - st[1]
            con = vertcat((cos_theta*dx+sin_theta*dy)/dt, (cos_theta*dy-sin_theta*dx)/dt, (st_next[2]-st[2])/dt)
            g.add_inequality(mtimes(H, con),
                ub = np.ones([4])*wheel_rad*w_max,
                lb =-np.ones([4])*wheel_rad*w_max
            )
            obj = obj + dt + P[6+k]*(mtimes((st-P[3:6]).T,mtimes(Q,(st-P[3:6]))) + 0.5*mtimes(mtimes(H, con).T, mtimes(H, con))) # calculate obj
            st = st_next
            
        # make the decision variables one column vector, these alternate between
        # states 
        OPT_variables = X + T

        opts = {
            'ipopt.max_iter': 200,
            'ipopt.print_level': 0,
            'print_time': 0,
            'ipopt.acceptable_tol': 1e-8,
            'ipopt.acceptable_obj_change_tol': 1e-6
        }

        nlp_prob = {'f':obj, 'x':OPT_variables.param, 'g':g.param, 'p':P}
        self.solver = nlpsol('solver', 'ipopt', nlp_prob, opts)

        self.args = {'lbg':g.lb,'ubg':g.ub,'lbx':OPT_variables.lb,'ubx':OPT_variables.ub}#, 'p':P, 'x0':P[0:3]}
        self.t0 = 0.1*np.ones((N, 1))


    def iterate_MPC(self, N):

        if not self.use_odom:
            self.pose_from_tf(self.tfListener) # Robot pose from /tf if use_odom is False

        x0 = np.array([[self.curx], [self.cury] , [self.curyaw]]);    # initial condition.
        xs = np.array([[self.gx], [self.gy], [self.gyaw]]); # Reference posture.
        #xs = np.array([[0], [0], [0]])

        via_points = np.zeros([N, 1])
        via_points[-1, 0] = 1.0
        if type(self.prev_path)!=type(None):
            for k in range(1, N):
                if (self.prev_path[N-k, 0]-self.gx)**2 + (self.prev_path[N-k, 1]-self.gy)**2 + (self.prev_path[N-k, 2]-self.gyaw)**2 < 0.01:
                    via_points[N-k-1, 0] = 1.0

        X0 = repmat(x0, 1, N+1)

        self.args['p'] = vertcat(x0, xs, via_points) # set the values of the parameters vector
        self.args['x0'] = vertcat(
            reshape(X0.T, 3*(N+1), 1),
            reshape(self.t0.T, N, 1)
        ) # initial value of the optimization variables, w0 is the optimization variable
        sol = self.solver(
            x0  = self.args['x0'],
            lbx = self.args['lbx'],
            ubx = self.args['ubx'],
            lbg = self.args['lbg'],
            ubg = self.args['ubg'],
            p   = self.args['p']
        )
     
        st = sol['x'][0:3]
        st_next = sol['x'][3:6]
        dt = sol['x'][3*(N+1)]
        cos_theta = np.cos(st[2])
        sin_theta = np.sin(st[2])
        dx = st_next[0] - st[0]
        dy = st_next[1] - st[1]
        u = [(cos_theta*dx+sin_theta*dy)/dt, (cos_theta*dy-sin_theta*dx)/dt, (st_next[2]-st[2])/dt]
        path_msg = Path()
        path_msg.header.frame_id = "map"
        path_msg.header.stamp = rospy.Time.now()
        for point in np.reshape(sol['x'][:3*(N+1)],[-1,3]):
            temp = PoseStamped()
            temp.header = path_msg.header
            temp.pose.position.x = point[0]
            temp.pose.position.y = point[1]
            quat = quaternion_from_euler(0.0, 0.0, point[2])
            temp.pose.orientation.z = quat[2]
            temp.pose.orientation.w = quat[3]
            path_msg.poses.append(temp)
        self.path_pub.publish(path_msg)
        self.prev_path = np.reshape(sol['x'][3:3*(N+1)], [N, 3])

        return u



if __name__ == '__main__':

    try:
        cmd_vel_topic='/cmd_vel'
        controller = MPC_controller(cmd_vel_topic)
        controller.spin(10)
    except rospy.ROSInterruptException:
        rospy.loginfo("node terminated.")

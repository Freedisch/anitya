# -*- coding: utf-8 -*-
# This file is a part of the Anitya project.
#
# Copyright © 2014-2017 Pierre-Yves Chibon <pingou@pingoured.fr>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
"""
anitya mapping of python classes to Database Tables.
"""

import collections
import datetime
import logging
import time

import six
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import (
    sessionmaker, scoped_session, query as sa_query, validates)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.types import TypeDecorator, CHAR
import uuid

from anitya.config import config as anitya_config
from .versions import GLOBAL_DEFAULT as DEFAULT_VERSION_SCHEME


#: This is a configured scoped session. It creates thread-local sessions. This
#: means that ``Session() is Session()`` is ``True``. This is a convenient way
#: to avoid passing a session instance around. Consult SQLAlchemy's documentation
#: for details.
#:
#: Before you can use this, you must call :func:`initialize`.
Session = scoped_session(sessionmaker())


def initialize(config):
    """
    Initialize the database.

    This creates a database engine from the provided configuration and
    configures the scoped session to use the engine.

    Args:
        config (dict): A dictionary that contains the configuration necessary
            to initialize the database.

    Returns:
        sqlalchemy.engine: The database engine created from the configuration.
    """
    #: The SQLAlchemy database engine. This is constructed using the value of
    #: ``DB_URL`` in :mod:`anitya.config``.
    engine = sa.create_engine(config['DB_URL'], echo=config.get('SQL_DEBUG', False))
    # Source: http://docs.sqlalchemy.org/en/latest/dialects/sqlite.html#foreign-key-support
    if config['DB_URL'].startswith('sqlite:'):
        sa.event.listen(
            engine,
            'connect',
            lambda db_con, con_record: db_con.execute('PRAGMA foreign_keys=ON')
        )
    Session.configure(bind=engine)
    return engine


_Page = collections.namedtuple(
    '_Page', ('items', 'page', 'items_per_page', 'total_items'))


class Page(_Page):
    """
    A sub-class of namedtuple that represents a page.

    Attributes:
        items (object): The database objects from the query.
        page (int): The page number used for the query.
        items_per_page (int): The number of items per page.
        total_items (int): The total number of items in the database.
    """

    def as_dict(self):
        """
        Return a dictionary representing the page.

        Returns:
            dict: A dictionary representation of the page and its items, using
                the ``__json__`` method defined on the item objects.
        """
        return {
            u'items': [item.__json__() for item in self.items],
            u'page': self.page,
            u'items_per_page': self.items_per_page,
            u'total_items': self.total_items,
        }


class BaseQuery(sa_query.Query):
    """A base Query object that provides queries."""

    def paginate(self, page=None, items_per_page=None, order_by=None):
        """
        Retrieve a page of items.

        Args:
            page (int): the page number to retrieve. This page is 1-indexed and
                        defaults to 1.
            items_per_page (int): The number of items per page. This defaults
                                  to 25.
            order_by (sa.Column or tuple): One or more criterion by which to order
                                           the pages.

        Returns:
            Page: A namedtuple of the items.

        Raises:
            ValueError: If the page or items_per_page values are less than 1.
        """

        if page is None:
            page = 1
        if items_per_page is None:
            items_per_page = 25

        if page < 1:
            raise ValueError('page must be 1 or greater.')
        if items_per_page < 1:
            raise ValueError('items_per_page must be 1 or greater.')

        if not isinstance(order_by, tuple):
            order_by = (order_by,)

        q = self.order_by(*order_by)
        total_items = q.count()
        items = q.limit(items_per_page).offset(items_per_page * (page - 1)).all()
        return Page(
            items=items, page=page, total_items=total_items, items_per_page=items_per_page)


class AnityaBase(object):
    """
    Base class for the SQLAlchemy model base class.

    Attributes:
        query (sqlalchemy.orm.query.Query): a class property which produces a
            :class:`BaseQuery` object against the class and the current Session
            when called. Classes that want a customized Query class should
            sub-class :class:`BaseQuery` and explicitly set the query property
            on the model.
    """

    query = Session.query_property(query_cls=BaseQuery)


BASE = declarative_base(cls=AnityaBase)


_log = logging.getLogger(__name__)


def _paginate_query(query, page):
    ''' Paginate a given query to returned the specified page (if any).
    '''
    if page:
        try:
            page = int(page)
        except ValueError:
            page = None

    if page:
        limit = 50
        offset = (page - 1) * limit
        query = query.offset(offset).limit(limit)

    return query


class Log(BASE):
    ''' Simple table to store/log action occuring in the database. '''
    __tablename__ = 'logs'

    id = sa.Column(sa.Integer, primary_key=True)
    user = sa.Column(sa.String(200), index=True, nullable=False)
    project = sa.Column(sa.String(200), index=True, nullable=True)
    distro = sa.Column(sa.String(200), index=True, nullable=True)
    description = sa.Column(sa.Text, nullable=False)
    created_on = sa.Column(sa.DateTime, default=datetime.datetime.utcnow)

    def __init__(self, user, project=None, distro=None, description=None):
        ''' Constructor.
        '''
        self.user = user
        self.project = project
        self.distro = distro
        self.description = description

    @classmethod
    def insert(cls, session, user, project=None, distro=None,
               description=None):
        """ Insert the given log entry into the database.

        :arg session: the session to connect to the database with
        :arg user: the username of the user doing the action
        :arg project: the `Project` object of the project changed
        :arg distro: the `Distro` object of the distro changed
        :arg description: a short textual description of the action
            performed

        """
        project_name = None
        if project:
            project_name = project.name

        distro_name = None
        if distro:
            distro_name = distro.name

        log = Log(user=user, project=project_name, distro=distro_name,
                  description=description)
        session.add(log)
        session.flush()

    @classmethod
    def search(cls, session, project_name=None, from_date=None, user=None,
               limit=None, offset=None, count=False):
        """ Return the list of the last Log entries present in the database.

        :arg cls: the class object
        :arg session: the database session used to query the information.
        :kwarg project_name: the name of the project to restrict the logs to.
        :kwarg from_date: the date from which to give the entries.
        :kwarg user: the name of the user to restrict the logs to.
        :kwarg limit: limit the result to X rows.
        :kwarg offset: start the result at row X.
        :kwarg count: a boolean to return the result of a COUNT query
            if true, returns the data if false (default).

        """
        query = session.query(
            cls
        )

        if project_name:
            query = query.filter(cls.project == project_name)

        if from_date:
            query = query.filter(cls.created_on >= from_date)

        if user:
            if isinstance(user, (list, tuple)):
                query = query.filter(cls.user.in_(user))
            else:
                query = query.filter(cls.user == user)

        query = query.order_by(cls.created_on.desc())

        if count:
            return query.count()

        if offset:
            query = query.offset(offset)
        if limit:
            query = query.limit(limit)

        return query.all()


class Distro(BASE):
    __tablename__ = 'distros'

    name = sa.Column(sa.String(200), primary_key=True)

    def __init__(self, name):
        ''' Constructor. '''
        self.name = name

    def __json__(self):
        return dict(name=self.name)

    @classmethod
    def by_name(cls, session, name):
        query = session.query(
            cls
        ).filter(
            sa.func.lower(cls.name) == sa.func.lower(name)
        )

        return query.first()

    get = by_name

    @classmethod
    def all(cls, session, page=None, count=False):
        query = session.query(cls).order_by(cls.name)

        query = _paginate_query(query, page)

        if count:
            return query.count()
        else:
            return query.all()

    @classmethod
    def search(cls, session, pattern, page=None, count=False):
        ''' Search the distribuutions by their name '''

        if '*' in pattern:
            pattern = pattern.replace('*', '%')

        query = session.query(
            cls
        ).filter(
            sa.or_(
                sa.func.lower(cls.name).like(sa.func.lower(pattern)),
            )
        ).order_by(
            cls.name
        ).distinct()

        query = _paginate_query(query, page)

        if count:
            return query.count()
        else:
            return query.all()

    @classmethod
    def get_or_create(cls, session, name):
        distro = cls.by_name(session, name)
        if not distro:
            distro = cls(
                name=name
            )
            session.add(distro)
            session.flush()
        return distro


class Packages(BASE):
    __tablename__ = 'packages'

    id = sa.Column(sa.Integer, primary_key=True)
    distro = sa.Column(
        sa.String(200),
        sa.ForeignKey(
            "distros.name",
            ondelete="cascade",
            onupdate="cascade"))
    project_id = sa.Column(
        sa.Integer,
        sa.ForeignKey(
            "projects.id",
            ondelete="cascade",
            onupdate="cascade")
    )

    package_name = sa.Column(sa.String(200))

    __table_args__ = (
        sa.UniqueConstraint('distro', 'package_name'),
    )

    project = sa.orm.relation('Project')

    def __repr__(self):
        return '<Packages(%s, %s: %s)>' % (
            self.project_id, self.distro, self.package_name)

    def __json__(self):
        return dict(
            package_name=self.package_name,
            distro=self.distro,
        )

    @classmethod
    def by_id(cls, session, pkg_id):
        return session.query(cls).filter_by(id=pkg_id).first()

    @classmethod
    def get(cls, session, project_id, distro, package_name):
        query = session.query(
            cls
        ).filter(
            cls.project_id == project_id
        ).filter(
            sa.func.lower(cls.distro) == sa.func.lower(distro)
        ).filter(
            cls.package_name == package_name
        )
        return query.first()

    @classmethod
    def by_package_name_distro(cls, session, package_name, distro):
        query = session.query(
            cls
        ).filter(
            cls.package_name == package_name
        ).filter(
            sa.func.lower(cls.distro) == sa.func.lower(distro)
        )
        return query.first()


class Project(BASE):
    """
    Models an upstream project and maps it to a database table.

    Attributes:
        id (sa.Integer): The database primary key.
        name (sa.String): The upstream project's name.
        homepage (sa.String): The URL for the project's home page.
        backend (sa.String): The name of the backend to use when fetching updates;
            this is a foreign key to a :class:`Backend`.
        ecosystem_name (sa.String): The name of the ecosystem this project is a part
            of. This is a foreign key to :class:`Ecosystem` and may be null.
        version_url (sa.String): The url to use when polling for new versions. This
            may be ignored if this project is part of an ecosystem with a fixed
            URL (e.g. Cargo projects are on https://crates.io).
        regex (sa.String): A Python ``re`` style regular expression that is applied
            to the HTML from ``version_url`` to find versions.
        insecure (sa.Boolean): Whether or not to validate the x509 certificate
            offered by the server at ``version_url``. Defaults to ``False``.
        latest_version (sa.Boolean): The latest version for the project, as determined
            by the version sorting algorithm.
        logs (sa.Text): The result of the last update.
        updated_on (sa.DateTime): When the project was last updated.
        created_on (sa.DateTime): When the project was created in Anitya.
        packages (list): List of :class:`Package` objects which represent the
            downstream packages for this project.
        version_scheme (sa.String): The version scheme to use for this project.
            If this is null, a default will be used. See the :mod:`anitya.lib.versions`
            documentation for more information.
    """
    __tablename__ = 'projects'

    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(200), nullable=False, index=True)
    homepage = sa.Column(sa.String(200), nullable=False)

    backend = sa.Column(sa.String(200), default='custom')
    ecosystem_name = sa.Column(sa.String(200), nullable=True, index=True)
    version_url = sa.Column(sa.String(200), nullable=True)
    regex = sa.Column(sa.String(200), nullable=True)
    version_prefix = sa.Column(sa.String(200), nullable=True)
    insecure = sa.Column(sa.Boolean, nullable=False, default=False)
    version_scheme = sa.Column(sa.String(50), nullable=True)

    latest_version = sa.Column(sa.String(50))
    logs = sa.Column(sa.Text)

    updated_on = sa.Column(sa.DateTime, server_default=sa.func.now(),
                           onupdate=sa.func.current_timestamp())
    created_on = sa.Column(sa.DateTime, default=datetime.datetime.utcnow)

    packages = sa.orm.relation('Packages')

    __table_args__ = (
        sa.UniqueConstraint('name', 'homepage'),
        sa.UniqueConstraint('name', 'ecosystem_name',
                            name="UNIQ_PROJECT_NAME_PER_ECOSYSTEM"),
    )

    @validates('backend')
    def validate_backend(self, key, value):
        # At the moment I have to stash this here because there's a circular
        # import. It can be resolved after the config is decoupled from Flask:
        # https://github.com/release-monitoring/anitya/pull/450
        from .plugins import BACKEND_PLUGINS
        if value not in BACKEND_PLUGINS.get_plugin_names():
            raise ValueError('Backend "{}" is not supported.'.format(value))
        return value

    @validates('ecosystem_name')
    def validate_ecosystem_name(self, key, value):
        # At the moment I have to stash this here because there's a circular
        # import. It can be resolved after the config is decoupled from Flask:
        # https://github.com/release-monitoring/anitya/pull/450
        from .plugins import ECOSYSTEM_PLUGINS
        if value and value not in ECOSYSTEM_PLUGINS.get_plugin_names():
            raise ValueError('Ecosystem "{}" is not supported.'.format(value))
        return value

    @property
    def versions(self):
        ''' Return list of all versions stored, sorted from newest to oldest.
        '''
        version_class = self.get_version_class()
        versions = [
            version_class(version=v_obj.version, prefix=self.version_prefix)
            for v_obj in self.versions_obj
        ]
        sorted_versions = reversed(sorted(versions))
        return [v.version for v in sorted_versions]

    def get_version_class(self):
        """
        Get the class for the version scheme used by this project.

        This will take into account the defaults set in the ecosystem, backend,
        and globally. The version scheme locations are checked in the following
        order and the first non-null result is returned:

        1. On the project itself in the ``version_scheme`` column.
        2. The project's ecosystem default, if the project is part of one.
        3. The project's backend default, if the backend defines one.
        4. The global default defined in :data:`anitya.lib.versions.GLOBAL_DEFAULT`

        Returns:
            anitya.lib.versions.Version: A ``Version`` sub-class.
        """
        from .plugins import ECOSYSTEM_PLUGINS, BACKEND_PLUGINS, VERSION_PLUGINS
        version_scheme = self.version_scheme
        if not version_scheme and self.ecosystem_name:
            ecosystem = ECOSYSTEM_PLUGINS.get_plugin(self.ecosystem_name)
            version_scheme = ecosystem.default_version_scheme
        if not version_scheme and self.backend:
            backend = BACKEND_PLUGINS.get_plugin(self.backend)
            version_scheme = backend.default_version_scheme
        if not version_scheme:
            version_scheme = DEFAULT_VERSION_SCHEME

        return VERSION_PLUGINS.get_plugin(version_scheme)

    def __repr__(self):
        return '<Project(%s, %s)>' % (self.name, self.homepage)

    def __json__(self, detailed=False):
        output = dict(
            id=self.id,
            name=self.name,
            homepage=self.homepage,
            regex=self.regex,
            backend=self.backend,
            version_url=self.version_url,
            version=self.latest_version,
            versions=self.versions,
            created_on=time.mktime(self.created_on.timetuple()) if self.created_on else None,
            updated_on=time.mktime(self.updated_on.timetuple()) if self.updated_on else None,
        )
        if detailed:
            output['packages'] = [pkg.__json__() for pkg in self.packages]

        return output

    @classmethod
    def get_or_create(cls, session, name, homepage, backend='custom'):
        project = cls.by_name_and_homepage(session, name, homepage)
        if not project:
            project = cls(name=name, homepage=homepage, backend=backend)
            session.add(project)
            session.flush()
        return project

    @classmethod
    def by_name(cls, session, name):
        return session.query(cls).filter_by(name=name).all()

    @classmethod
    def by_id(cls, session, project_id):
        return session.query(cls).filter_by(id=project_id).first()

    get = by_id

    @classmethod
    def by_homepage(cls, session, homepage):
        return session.query(cls).filter_by(homepage=homepage).all()

    @classmethod
    def by_name_and_homepage(cls, session, name, homepage):
        query = session.query(
            cls
        ).filter(
            cls.name == name
        ).filter(
            cls.homepage == homepage
        )
        return query.first()

    @classmethod
    def by_name_and_ecosystem(cls, session, name, ecosystem):
        try:
            query = session.query(cls)
            query = query.filter(cls.name == name, cls.ecosystem_name == ecosystem)
            return query.one()
        except NoResultFound:
            return None

    @classmethod
    def all(cls, session, page=None, count=False):
        query = session.query(
            Project
        ).order_by(
            sa.func.lower(Project.name)
        )

        query = _paginate_query(query, page)

        if count:
            return query.count()
        else:
            return query.all()

    @classmethod
    def by_distro(cls, session, distro, page=None, count=False):
        query = session.query(
            Project
        ).filter(
            Project.id == Packages.project_id
        ).filter(
            sa.func.lower(Packages.distro) == sa.func.lower(distro)
        ).order_by(
            sa.func.lower(Project.name)
        )

        query = _paginate_query(query, page)

        if count:
            return query.count()
        else:
            return query.all()

    @classmethod
    def updated(
            cls, session, status='updated', name=None, log=None,
            page=None, count=False):
        ''' Method used to retrieve projects according to their logs and
        how they performed at the last cron job.

        :kwarg status: used to filter the projects based on how they
            performed at the last cron run
        :kwarg name: if present, will return the entries having the matching
            name
        :kwarg log: if present, will return the entries having the matching
            log
        :kwarg page: The page number of returned, pages contain 50 entries
        :kwarg count: A boolean used to return either the list of entries
            matching the criterias or just the COUNT of entries

        '''

        query = session.query(
            Project
        ).order_by(
            sa.func.lower(Project.name)
        )

        if status == 'updated':
            query = query.filter(
                Project.logs.isnot(None),
                Project.logs == 'Version retrieved correctly',
            )
        elif status == 'failed':
            query = query.filter(
                Project.logs.isnot(None),
                Project.logs != 'Version retrieved correctly',
                ~Project.logs.ilike('Something strange occured%'),
            )
        elif status == 'odd':
            query = query.filter(
                Project.logs.isnot(None),
                Project.logs != 'Version retrieved correctly',
                Project.logs.ilike('Something strange occured%'),
            )

        elif status == 'new':
            query = query.filter(
                Project.logs.is_(None),
            )
        elif status == 'never_updated':
            query = query.filter(
                Project.logs.isnot(None),
                Project.logs != 'Version retrieved correctly',
                Project.latest_version.is_(None),
            )

        if name:
            if '*' in name:
                name = name.replace('*', '%')
            else:
                name = '%' + name + '%'

            query = query.filter(
                Project.name.ilike(name),
            )

        if log:
            if '*' in log:
                log = log.replace('*', '%')
            else:
                log = '%' + log + '%'

            query = query.filter(
                Project.logs.ilike(log),
            )

        query = _paginate_query(query, page)

        if count:
            return query.count()
        else:
            return query.all()

    @classmethod
    def search(cls, session, pattern, distro=None, page=None, count=False):
        ''' Search the projects by their name or package name '''

        query1 = session.query(
            cls
        )

        if pattern:
            pattern = pattern.replace('_', '\_')
            if '*' in pattern:
                pattern = pattern.replace('*', '%')
            if '%' in pattern:
                query1 = query1.filter(
                    Project.name.ilike(pattern)
                )
            else:
                query1 = query1.filter(
                    Project.name == pattern
                )

        query2 = session.query(
            cls
        ).filter(
            Project.id == Packages.project_id
        )

        if pattern:
            if '%' in pattern:
                query2 = query2.filter(
                    Packages.package_name.ilike(pattern)
                )
            else:
                query2 = query2.filter(
                    Packages.package_name == pattern
                )

        if distro is not None:
            query1 = query1.filter(
                Project.id == Packages.project_id
            ).filter(
                sa.func.lower(Packages.distro) == sa.func.lower(distro)
            )

            query2 = query2.filter(
                sa.func.lower(Packages.distro) == sa.func.lower(distro)
            )

        query = query1.distinct().union(
            query2.distinct()
        ).order_by(
            cls.name
        )

        query = _paginate_query(query, page)

        if count:
            return query.count()
        else:
            return query.all()


class ProjectVersion(BASE):
    __tablename__ = 'projects_versions'

    project_id = sa.Column(
        sa.Integer,
        sa.ForeignKey(
            "projects.id",
            ondelete="cascade",
            onupdate="cascade"),
        primary_key=True,
    )
    version = sa.Column(sa.String(50), primary_key=True)

    project = sa.orm.relation('Project', backref='versions_obj')


class ProjectFlag(BASE):
    __tablename__ = 'projects_flags'

    id = sa.Column(sa.Integer, primary_key=True)

    project_id = sa.Column(
        sa.Integer,
        sa.ForeignKey(
            "projects.id",
            ondelete="cascade",
            onupdate="cascade")
    )

    reason = sa.Column(sa.Text, nullable=False)
    user = sa.Column(sa.String(200), index=True, nullable=False)
    state = sa.Column(sa.String(50), default='open', nullable=False)
    created_on = sa.Column(sa.DateTime, default=datetime.datetime.utcnow)
    updated_on = sa.Column(sa.DateTime, server_default=sa.func.now(),
                           onupdate=sa.func.current_timestamp())

    project = sa.orm.relation('Project', backref='flags')

    def __repr__(self):
        return '<ProjectFlag(%s, %s, %s)>' % (self.project.name, self.user,
                                              self.state)

    def __json__(self, detailed=False):
        output = dict(
            id=self.id,
            project=self.project.name,
            user=self.user,
            state=self.state,
            created_on=time.mktime(self.created_on.timetuple()),
            updated_on=time.mktime(self.updated_on.timetuple()),
        )
        if detailed:
            output['reason'] = self.reason

        return output

    @classmethod
    def all(cls, session, page=None, count=False):
        query = session.query(
            ProjectFlag
        ).order_by(ProjectFlag.created_on)

        return query.all()

    @classmethod
    def search(cls, session, project_name=None, from_date=None, user=None,
               state=None, limit=None, offset=None, count=False):
        """ Return the list of the last Flag entries present in the database.

        :arg cls: the class object
        :arg session: the database session used to query the information.
        :kwarg project_name: the name of the project to restrict the flags to.
        :kwarg from_date: the date from which to give the entries.
        :kwarg user: the name of the user to restrict the flags to.
        :kwarg state: the flag's status (open or closed).
        :kwarg limit: limit the result to X rows.
        :kwarg offset: start the result at row X.
        :kwarg count: a boolean to return the result of a COUNT query
            if true, returns the data if false (default).

        """
        query = session.query(
            cls
        )

        if project_name:
            query = query.filter(
                cls.project_id == Project.id
            ) .filter(
                Project.name == project_name
            )

        if from_date:
            query = query.filter(cls.created_on >= from_date)

        if user:
            query = query.filter(cls.user == user)

        if state:
            query = query.filter(cls.state == state)

        query = query.order_by(cls.created_on.desc())

        if count:
            return query.count()

        if offset:
            query = query.offset(offset)
        if limit:
            query = query.limit(limit)

        return query.all()

    @classmethod
    def get(cls, session, flag_id):
        query = session.query(
            cls
        ).filter(
            cls.id == flag_id)
        return query.first()


class Run(BASE):
    __tablename__ = 'runs'

    status = sa.Column(sa.String(20), primary_key=True)
    created_on = sa.Column(
        sa.DateTime, default=datetime.datetime.utcnow, primary_key=True)

    @classmethod
    def last_entry(cls, session):
        ''' Return the last log about the cron run. '''

        query = session.query(
            cls
        ).order_by(
            cls.created_on.desc()
        )
        return query.first()


class GUID(TypeDecorator):
    """
    Platform-independent GUID type.

    If PostgreSQL is being used, use its native UUID type, otherwise use a CHAR(32) type.
    """
    impl = CHAR

    def load_dialect_impl(self, dialect):
        """
        PostgreSQL has a native UUID type, so use it if we're using PostgreSQL.

        Args:
            dialect (sqlalchemy.engine.interfaces.Dialect): The dialect in use.

        Returns:
            sqlalchemy.types.TypeEngine: Either a PostgreSQL UUID or a CHAR(32) on other
                dialects.
        """
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(UUID())
        else:
            return dialect.type_descriptor(CHAR(32))

    def process_bind_param(self, value, dialect):
        """
        Process the value being bound.

        If PostgreSQL is in use, just use the string representation of the UUID.
        Otherwise, use the integer as a hex-encoded string.

        Args:
            value (object): The value that's being bound to the object.
            dialect (sqlalchemy.engine.interfaces.Dialect): The dialect in use.

        Returns:
            str: The value of the UUID as a string.
        """
        if value is None:
            return value
        elif dialect.name == 'postgresql':
            return str(value)
        else:
            if not isinstance(value, uuid.UUID):
                return "%.32x" % uuid.UUID(value).int
            else:
                # hexstring
                return "%.32x" % value.int

    def process_result_value(self, value, dialect):
        """
        Casts the UUID value to the native Python type.

        Args:
            value (object): The database value.
            dialect (sqlalchemy.engine.interfaces.Dialect): The dialect in use.

        Returns:
            uuid.UUID: The value as a Python :class:`uuid.UUID`.
        """
        if value is None:
            return value
        else:
            return uuid.UUID(value)


class User(BASE):
    """
    A table for Anitya users.

    This table is intended to work with a table of third-party authentication
    providers. Anitya does not support local users.

    Attributes:
        id (uuid.UUID): The primary key for the table.
        email (str): The user's email.
        username (str): The user's username, as retrieved from third-party authentication.
        active (bool): Indicates whether the user is active. If false, users will not be
            able to log in.
        social_auth (sqlalchemy.orm.dynamic.AppenderQuery): The list of
            :class:`social_flask_sqlalchemy.models.UserSocialAuth` entries for this user.
    """
    __tablename__ = 'users'

    id = sa.Column(GUID, primary_key=True, default=uuid.uuid4)
    # SMTP says 256 is the maximum length of a path:
    # https://tools.ietf.org/html/rfc5321#section-4.5.3
    email = sa.Column(sa.String(256), nullable=False, index=True, unique=True)
    username = sa.Column(sa.String(256), nullable=False, index=True, unique=True)
    active = sa.Column(sa.Boolean, default=True)

    @property
    def admin(self):
        """
        Determine if this user is an administrator.

        Returns:
            bool: True if the user is an administrator.
        """
        return six.text_type(self.id) in anitya_config.get('ANITYA_WEB_ADMINS', [])

    @property
    def is_active(self):
        """
        Implement the flask-login interface for determining if the user is active.

        If a user is _not_ active, they are not allowed to log in.

        Returns:
            bool: True if the user is active.
        """
        return self.active

    @property
    def is_anonymous(self):
        """
        Implement the flask-login interface for determining if the user is authenticated.

        flask-login uses an "anonymous user" object if there is no authenticated user. This
        indicates to flask-login this user is not an anonymous user.

        Returns:
            bool: False in all cases.
        """
        return False

    @property
    def is_authenticated(self):
        """
        Implement the flask-login interface for determining if the user is authenticated.

        In this case, if flask-login has an instance of :class:`User`, then that user has
        already authenticated via a third-party authentication mechanism.

        Returns:
            bool: True in all cases.
        """
        return True

    def get_id(self):
        """
        Implement the flask-login interface for retrieving the user's ID.

        Returns:
            six.text_type: The Unicode string that uniquely identifies a user.
        """
        return six.text_type(self.id)

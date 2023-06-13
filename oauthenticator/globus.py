"""
Custom Authenticator to use Globus OAuth2 with JupyterHub
"""
import base64
import os
import pickle
import urllib

from jupyterhub.auth import LocalAuthenticator
from tornado.web import HTTPError
from traitlets import Bool, List, Set, Unicode, default

from .oauth2 import OAuthenticator, OAuthLogoutHandler


class GlobusLogoutHandler(OAuthLogoutHandler):
    """
    Handle custom logout URLs and token revocation. If a custom logout url
    is specified, the 'logout' button will log the user out of that identity
    provider in addition to clearing the session with Jupyterhub, otherwise
    only the Jupyterhub session is cleared.
    """

    async def get(self):
        # Ensure self.handle_logout() is called before self.default_handle_logout()
        # If default_handle_logout() is called first, the user session is popped and
        # it's not longer possible to call get_auth_state() to revoke tokens.
        # See https://github.com/jupyterhub/jupyterhub/blob/HEAD/jupyterhub/handlers/login.py  # noqa
        await self.handle_logout()
        await self.default_handle_logout()
        if self.authenticator.logout_redirect_url:
            # super().get() will attempt to render a logout page. Make sure we
            # return after the redirect to avoid exceptions.
            self.redirect(self.authenticator.logout_redirect_url)
            return
        await super().get()

    async def handle_logout(self):
        """Overridden method for custom logout functionality. Should be called by
        Jupyterhub on logout just before destroying the users session to log them out.
        """
        await super().handle_logout()

        if self.current_user and self.authenticator.revoke_tokens_on_logout:
            await self.clear_tokens(self.current_user)

    async def clear_tokens(self, user):
        """Revoke and clear user tokens from the database"""
        state = await user.get_auth_state()
        if state:
            await self.authenticator.revoke_service_tokens(state.get('tokens'))
            self.log.info(
                'Logout: Revoked tokens for user "{}" services: {}'.format(
                    user.name, ','.join(state['tokens'].keys())
                )
            )
            state['tokens'] = {}
            await user.save_auth_state(state)


class GlobusOAuthenticator(OAuthenticator):
    """The Globus OAuthenticator handles both authorization and passing
    transfer tokens to the spawner."""

    login_service = 'Globus'
    logout_handler = GlobusLogoutHandler

    user_auth_state_key = "globus_user"

    @default("userdata_url")
    def _userdata_url_default(self):
        return "https://auth.globus.org/v2/oauth2/userinfo"

    @default("authorize_url")
    def _authorize_url_default(self):
        return "https://auth.globus.org/v2/oauth2/authorize"

    @default("token_url")
    def _token_url_default(self):
        return "https://auth.globus.org/v2/oauth2/token"

    revocation_url = Unicode(
        "https://auth.globus.org/v2/oauth2/token/revoke",
        help="Globus URL to revoke live tokens.",
        config=True,
    )
    globus_groups_url = Unicode(
        "https://groups.api.globus.org/v2/groups/my_groups",
        help="Globus URL to get list of user's Groups.",
        config=True,
    )

    identity_provider = Unicode(
        help="""Restrict which institution a user
    can use to login (GlobusID, University of Hogwarts, etc.). This should
    be set in the app at developers.globus.org, but this acts as an additional
    check to prevent unnecessary account creation."""
    ).tag(config=True)

    def _identity_provider_default(self):
        return os.getenv('IDENTITY_PROVIDER', '')

    username_from_email = Bool(
        False,
        help="""Create username from email address, not preferred username. If
        an identity provider is specified, email address must be from the same
        domain. Email scope will be set automatically.""",
        config=True,
    )

    @default("username_claim")
    def _username_claim_default(self):
        if self.username_from_email:
            return "email"
        return "preferred_username"

    exclude_tokens = List(
        help="""Exclude tokens from being passed into user environments
        when they start notebooks, Terminals, etc."""
    ).tag(config=True)

    def _exclude_tokens_default(self):
        return ['auth.globus.org', 'groups.api.globus.org']

    def _scope_default(self):
        scopes = [
            'openid',
            'profile',
            'urn:globus:auth:scope:transfer.api.globus.org:all',
        ]
        if self.allowed_globus_groups or self.admin_globus_groups:
            scopes.append(
                'urn:globus:auth:scope:groups.api.globus.org:view_my_groups_and_memberships'
            )
        if self.username_from_email:
            scopes.append('email')
        return scopes

    globus_local_endpoint = Unicode(
        help="""If Jupyterhub is also a Globus
    endpoint, its endpoint id can be specified here."""
    ).tag(config=True)

    def _globus_local_endpoint_default(self):
        return os.getenv('GLOBUS_LOCAL_ENDPOINT', '')

    revoke_tokens_on_logout = Bool(
        help="""Revoke tokens so they cannot be used again. Single-user servers
        MUST be restarted after logout in order to get a fresh working set of
        tokens."""
    ).tag(config=True)

    def _revoke_tokens_on_logout_default(self):
        return False

    allowed_globus_groups = Set(
        help="""Allow members of defined Globus Groups to access JupyterHub. Users in an
        admin Globus Group are also automatically allowed. Groups are specified with their UUIDs. Setting this will
        add the Globus Groups scope."""
    ).tag(config=True)

    admin_globus_groups = Set(
        help="""Set members of defined Globus Groups as JupyterHub admin users.
        These users are automatically allowed to login to JupyterHub. Groups are specified with
        their UUIDs. Setting this will add the Globus Groups scope."""
    ).tag(config=True)

    @staticmethod
    def check_user_in_groups(member_groups, allowed_groups):
        return bool(set(member_groups) & set(allowed_groups))

    async def pre_spawn_start(self, user, spawner):
        """Add tokens to the spawner whenever the spawner starts a notebook.
        This will allow users to create a transfer client:
        globus-sdk-python.readthedocs.io/en/stable/tutorial/#tutorial-step4
        """
        spawner.environment['GLOBUS_LOCAL_ENDPOINT'] = self.globus_local_endpoint
        state = await user.get_auth_state()
        if state:
            globus_data = base64.b64encode(pickle.dumps(state))
            spawner.environment['GLOBUS_DATA'] = globus_data.decode('utf-8')

    def get_globus_tokens(self, token_info):
        # Each token should have these attributes. Resource server is optional,
        # and likely won't be present.
        token_attrs = [
            'expires_in',
            'resource_server',
            'scope',
            'token_type',
            'refresh_token',
            'access_token',
        ]
        # The Auth Token is a bit special, it comes back at the top level with the
        # id token. The id token has some useful information in it, but nothing that
        # can't be retrieved with an Auth token.
        # Repackage the Auth token into a dict that looks like the other tokens
        auth_token_dict = {
            attr_name: token_info.get(attr_name) for attr_name in token_attrs
        }
        # Make sure only the essentials make it into tokens. Other items, such as 'state' are
        # not needed after authentication and can be discarded.
        other_tokens = [
            {attr_name: token_dict.get(attr_name) for attr_name in token_attrs}
            for token_dict in token_info['other_tokens']
        ]
        return other_tokens + [auth_token_dict]

    def build_auth_state_dict(self, token_info, user_info):
        """
        Usernames (and therefore Jupyterhub
        accounts) will correspond to a Globus User ID, so foouser@globusid.org
        will have the 'foouser' account in Jupyterhub.
        """

        tokens = self.get_globus_tokens(token_info)
        # historically, tokens have been organized by resource server for convenience.
        # If multiple scopes are requested from the same resource server, they will be
        # combined into a single token from Globus Auth.
        by_resource_server = {
            token_dict['resource_server']: token_dict
            for token_dict in tokens
            if token_dict['resource_server'] not in self.exclude_tokens
        }

        return {
            'client_id': self.client_id,
            'tokens': by_resource_server,
            'token_response': token_info,
            self.user_auth_state_key: user_info,
        }

    # FIXME: Should we persist info about user groups in auth model
    #        to be consistent with what's happening in bitbucket.py
    #        where the `auth_model` is updated with `user_teams`.
    async def get_users_groups_ids(self, tokens):
        user_group_ids = set()
        # Get Groups access token, may not be in dict headed to auth state
        for token_dict in tokens:
            if token_dict['resource_server'] == 'groups.api.globus.org':
                groups_token = token_dict['access_token']
        # Get list of user's Groups
        groups_headers = self.get_default_headers()
        groups_headers['Authorization'] = f'Bearer {groups_token}'
        groups_resp = await self.httpfetch(
            self.globus_groups_url, method='GET', headers=groups_headers
        )
        # Build set of Group IDs
        for group in groups_resp:
            user_group_ids.add(group['id'])

        return user_group_ids

    async def check_allowed(self, username, auth_model):
        """
        Returns True for users allowed to be authorized.

        Overrides the OAuthenticator.check_allowed implementation to allow users
        either part of `allowed_users` or `allowed_globus_groups`, and not just those
        part of `allowed_users`.
        """
        # allow admin users recognized via admin_users or update_auth_model
        if auth_model["admin"]:
            return True

        # FIXME: consider overriding the `is_admin`` and add this logic there
        tokens = self.get_globus_tokens(auth_model["auth_state"]["token_response"])
        user_group_ids = await self.get_users_groups_ids(tokens)
        if self.admin_globus_groups:
            if self.check_user_in_groups(user_group_ids, self.admin_globus_groups):
                auth_model["admin"] = True
                return True

        if self.identity_provider:
            # It's possible for identity provider domains to be namespaced
            # https://docs.globus.org/api/auth/specification/#identity_provider_namespaces
            user_info = auth_model["auth_state"][self.user_auth_state_key]
            domain = user_info.get(self.username_claim).split('@', 1)[-1]
            if domain != self.identity_provider:
                self.log.error(
                    f"Trying to login from an identity provider that was not allowed {domain}",
                )
                raise HTTPError(
                    403,
                    f"This site is restricted to {self.identity_provider} accounts. "
                    "Please link your account at app.globus.org/account.",
                )

        # if allowed_users or allowed_globus_groups is configured, we deny users not part of either
        if self.allowed_users or self.allowed_globus_groups:
            if username in self.allowed_users:
                return True

            if self.allowed_globus_groups:
                if self.check_user_in_groups(
                    user_group_ids, self.allowed_globus_groups
                ):
                    return True

                self.log.warning(f"{username} not in an allowed Globus Group")

            return False

        # otherwise, authorize all users
        return True

    async def update_auth_model(self, auth_model):
        # If users is already marked as an admin, it means that
        # they are present in the `admin_users`` list
        # no need to check groups membership
        if auth_model['admin'] is True:
            return auth_model

        if self.admin_globus_groups:
            tokens = self.get_globus_tokens(auth_model["auth_state"]["token_response"])
            # If any of these configurations are set, user must be in the allowed or admin Globus Group
            user_group_ids = await self.get_users_groups_ids(tokens)
            # Admin users are being managed via Globus Groups
            # Default to False
            auth_model['admin'] = False
            if self.check_user_in_groups(user_group_ids, self.admin_globus_groups):
                auth_model['admin'] = True

        return auth_model

    def user_info_to_username(self, user_info):
        """
        Usernames (and therefore Jupyterhub
        accounts) will correspond to a Globus User ID, so foouser@globusid.org
        will have the 'foouser' account in Jupyterhub.
        """

        return user_info.get(self.username_claim).split('@')[0]

    def get_default_headers(self):
        return {"Accept": "application/json", "User-Agent": "JupyterHub"}

    def get_client_credential_headers(self):
        headers = self.get_default_headers()
        b64key = base64.b64encode(
            bytes(f"{self.client_id}:{self.client_secret}", "utf8")
        )
        headers["Authorization"] = "Basic {}".format(b64key.decode("utf8"))
        return headers

    async def revoke_service_tokens(self, services):
        """
        Revoke live Globus access and refresh tokens.

        Revoking inert or non-existent tokens does nothing.
        Services are defined by dicts returned by `tokens.by_resource_server`.

        For example::

            services = {
                'transfer.api.globus.org': {'access_token': 'token'},
                <Additional services>...
            }
        """

        access_tokens = [
            token_dict.get('access_token') for token_dict in services.values()
        ]
        refresh_tokens = [
            token_dict.get('refresh_token') for token_dict in services.values()
        ]
        all_tokens = [tok for tok in access_tokens + refresh_tokens if tok is not None]

        for token in all_tokens:
            await self.httpfetch(
                self.revocation_url,
                method="POST",
                headers=self.get_client_credential_headers(),
                body=urllib.parse.urlencode({'token': token}),
            )


class LocalGlobusOAuthenticator(LocalAuthenticator, GlobusOAuthenticator):
    """A version that mixes in local system user creation"""
